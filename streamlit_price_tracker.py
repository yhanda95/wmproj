"""
Streamlit Price Tracker — single-file app

Files produced here:
- streamlit_price_tracker.py  (this file)

Requirements (pip):
streamlit
requests
beautifulsoup4
sqlalchemy
apscheduler
pandas
plotly
python-dotenv

Install:
pip install streamlit requests beautifulsoup4 sqlalchemy apscheduler pandas plotly python-dotenv

Run:
streamlit run streamlit_price_tracker.py

Notes:
- This is a minimal, extendable single-file prototype that
  - stores data in SQLite (file: prices.db)
  - scrapes prices for basic Amazon and Flipkart product pages (selectors may need updates)
  - schedules periodic scraping with APScheduler
  - sends email notifications via SMTP when price <= user threshold
  - provides a Streamlit dashboard to add products, view current prices and historical trends

Security & production:
- For production scraping at scale, add proxy rotation, request throttling, headers, and respect robots.txt
- For emails use a transactional provider (SendGrid, Mailgun) instead of direct SMTP in production
- Use environment variables for secrets (SMTP credentials). A .env file is supported via python-dotenv.

"""

import streamlit as st
import requests
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, MetaData, Table
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from apscheduler.schedulers.background import BackgroundScheduler
import pandas as pd
import plotly.express as px
from datetime import datetime
import time
import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

load_dotenv()

# -------------------------------
# Configuration
# -------------------------------
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///prices.db')
SCRAPE_INTERVAL_MINUTES = int(os.getenv('SCRAPE_INTERVAL_MINUTES', '360'))  # default every 6 hours
SMTP_HOST = os.getenv('SMTP_HOST', '')
SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER = os.getenv('SMTP_USER', '')
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD', '')
FROM_EMAIL = os.getenv('FROM_EMAIL', SMTP_USER)

# -------------------------------
# Database setup (SQLAlchemy)
# -------------------------------
Base = declarative_base()

class Product(Base):
    __tablename__ = 'products'
    id = Column(Integer, primary_key=True)
    name = Column(String)
    url = Column(String, unique=True)
    desired_price = Column(Float, nullable=True)
    notify_email = Column(String, nullable=True)

class PriceHistory(Base):
    __tablename__ = 'price_history'
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer)
    price = Column(Float)
    timestamp = Column(DateTime)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
Base.metadata.create_all(bind=engine)

# -------------------------------
# Scraper helpers
# -------------------------------
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
}


def parse_price_text(text):
    """Try to extract a float from a price string."""
    if not text:
        return None
    # remove commas, currency symbols, and non-numeric chars except dot
    filtered = ''.join(ch for ch in text if (ch.isdigit() or ch == '.' or ch == ','))
    if not filtered:
        return None
    # remove commas
    filtered = filtered.replace(',', '')
    try:
        return float(filtered)
    except Exception:
        return None


def scrape_amazon_price(soup: BeautifulSoup):
    # Amazon has varied selectors; we try a few common ones
    selectors = [
        '#priceblock_ourprice',
        '#priceblock_dealprice',
        '.a-price .a-offscreen'
    ]
    for sel in selectors:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            price = parse_price_text(el.get_text())
            if price:
                return price
    return None


def scrape_flipkart_price(soup: BeautifulSoup):
    # Flipkart common selectors
    selectors = [
        'div._30jeq3._16Jk6d',
        'div._30jeq3'
    ]
    for sel in selectors:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            price = parse_price_text(el.get_text())
            if price:
                return price
    return None


def detect_site_and_scrape(url: str):
    """Fetch URL and attempt to parse price with site-specific rules.
    Returns (name, price) where name is product title if found.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return None, None
        soup = BeautifulSoup(resp.text, 'html.parser')
        title = None
        title_el = soup.find('title')
        if title_el:
            title = title_el.get_text(strip=True)

        # Basic domain checks
        if 'amazon.' in url:
            price = scrape_amazon_price(soup)
            return title, price
        if 'flipkart.' in url:
            price = scrape_flipkart_price(soup)
            return title, price

        # Generic attempt: look for meta price or common price patterns
        meta_price = soup.select_one('meta[itemprop="price"]')
        if meta_price and meta_price.get('content'):
            price = parse_price_text(meta_price.get('content'))
            return title, price

        # Fallback: try to find common price-looking text
        possible = soup.find_all(text=True)
        for t in possible[-200:]:  # search near end of page first
            txt = t.strip()
            if txt and any(c.isdigit() for c in txt) and ('₹' in txt or '$' in txt or 'Rs.' in txt):
                price = parse_price_text(txt)
                if price:
                    return title, price
        return title, None
    except Exception as e:
        print('Scrape error for', url, e)
        return None, None

# -------------------------------
# Notification
# -------------------------------

def send_email_notification(to_email: str, product_name: str, url: str, old_price: float, new_price: float):
    if not SMTP_USER or not SMTP_PASSWORD or not SMTP_HOST:
        print('SMTP not configured; skipping email')
        return False
    try:
        subject = f'Price drop alert: {product_name} now {new_price}'
        body = f"""Good news!

The product '{product_name}' has dropped in price.

Old price: {old_price}\nNew price: {new_price}\nLink: {url}

-- Price Tracker
"""
        msg = MIMEMultipart()
        msg['From'] = FROM_EMAIL
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(FROM_EMAIL, to_email, msg.as_string())
        server.quit()
        print('Email sent to', to_email)
        return True
    except Exception as e:
        print('Failed to send email', e)
        return False

# -------------------------------
# Core scheduled job: update prices
# -------------------------------

def update_all_prices():
    print('Running scheduled price update at', datetime.utcnow())
    session = SessionLocal()
    products = session.query(Product).all()
    for p in products:
        title, price = detect_site_and_scrape(p.url)
        if price is None:
            print('Could not find price for', p.url)
            continue
        # get last price
        last = session.query(PriceHistory).filter(PriceHistory.product_id == p.id).order_by(PriceHistory.timestamp.desc()).first()
        last_price = last.price if last else None
        # insert history
        ph = PriceHistory(product_id=p.id, price=price, timestamp=datetime.utcnow())
        session.add(ph)
        session.commit()
        print(f'Updated {p.name or title} -> {price} (was {last_price})')
        # check threshold
        if p.desired_price is not None and price <= p.desired_price:
            # If last_price is None or price < last_price -> notify
            if (last_price is None) or (price < last_price):
                if p.notify_email:
                    send_email_notification(p.notify_email, p.name or title or 'Product', p.url, last_price, price)
    session.close()

# run scheduler in background
scheduler = BackgroundScheduler()
scheduler.add_job(update_all_prices, 'interval', minutes=SCRAPE_INTERVAL_MINUTES, id='price_updater', replace_existing=True)
scheduler.start()

# -------------------------------
# Streamlit UI
# -------------------------------

st.set_page_config(page_title='Price Tracker', layout='wide')
st.title('E‑commerce Price Tracker — Streamlit Prototype')

# Sidebar: add a product
st.sidebar.header('Add product to track')
with st.sidebar.form('add_product_form'):
    url = st.text_input('Product URL')
    desired_price = st.number_input('Desired price (optional)', min_value=0.0, value=0.0, step=1.0)
    notify_email = st.text_input('Email to notify (optional)')
    submit = st.form_submit_button('Add / Update')

if submit:
    if not url:
        st.sidebar.error('Please provide a product URL')
    else:
        session = SessionLocal()
        # fetch initial info
        title, price = detect_site_and_scrape(url)
        name = title or url
        # upsert product
        prod = session.query(Product).filter(Product.url == url).first()
        if prod:
            prod.name = name
            prod.desired_price = desired_price if desired_price > 0 else None
            prod.notify_email = notify_email or None
            session.commit()
            st.sidebar.success('Product updated')
        else:
            newp = Product(name=name, url=url, desired_price=(desired_price if desired_price > 0 else None), notify_email=(notify_email or None))
            session.add(newp)
            session.commit()
            # add current price to history
            if price is not None:
                ph = PriceHistory(product_id=newp.id, price=price, timestamp=datetime.utcnow())
                session.add(ph)
                session.commit()
            st.sidebar.success('Product added')
        session.close()

# Main area: list products
session = SessionLocal()
products = session.query(Product).all()

if not products:
    st.info('No products being tracked — add one from the sidebar')
else:
    cols = st.columns([3, 1, 1, 2])
    cols[0].markdown('**Product**')
    cols[1].markdown('**Current Price**')
    cols[2].markdown('**Desired**')
    cols[3].markdown('**Actions**')

    for p in products:
        # get latest price
        last = session.query(PriceHistory).filter(PriceHistory.product_id == p.id).order_by(PriceHistory.timestamp.desc()).first()
        last_price = last.price if last else None
        c0, c1, c2, c3 = st.columns([3, 1, 1, 2])
        c0.markdown(f"**{p.name}**  \n {p.url}")
        c1.markdown(f"{last_price if last_price is not None else '—'}")
        c2.markdown(f"{p.desired_price if p.desired_price is not None else '—'}")
        with c3:
            if st.button(f'View {p.id}', key=f'view_{p.id}'):
                # show history chart
                df = pd.read_sql_query(f"SELECT * FROM price_history WHERE product_id={p.id} ORDER BY timestamp", engine)
                if df.empty:
                    st.warning('No price history yet for this product')
                else:
                    df['timestamp'] = pd.to_datetime(df['timestamp'])
                    fig = px.line(df, x='timestamp', y='price', title=p.name or p.url)
                    st.plotly_chart(fig, use_container_width=True)
                    st.write(df[['timestamp','price']].tail(50))
            if st.button(f'Remove {p.id}', key=f'remove_{p.id}'):
                # delete product and its history
                session.query(PriceHistory).filter(PriceHistory.product_id == p.id).delete()
                session.delete(p)
                session.commit()
                st.experimental_rerun()

session.close()

st.markdown('---')
st.write('Scheduler running in background to update prices every', SCRAPE_INTERVAL_MINUTES, 'minutes.')

# Manual trigger
if st.button('Run price update now'):
    with st.spinner('Running update...'):
        update_all_prices()
        st.success('Update complete — refresh the page to see new prices')

st.markdown('## Notes & Troubleshooting')
st.markdown('''
- If a site's prices are not being detected, update the scraping selectors in the `scrape_amazon_price` or `scrape_flipkart_price` functions.
- For real-world use, add request throttling and rotate user agents and proxies to avoid being blocked.
- Use a transactional email service in production instead of SMTP login.
''')

# keep the scheduler alive in streamlit.
# streamlit will run this script top-to-bottom on reruns; we've started a BackgroundScheduler which will continue while the process is alive.

# end of file
