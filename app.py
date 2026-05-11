import os

import pandas as pd
import streamlit as st
from pyairtable import Table
from requests.exceptions import HTTPError

# --- Configuration & Connection ---
def get_config_value(key, env_name, default=""):
    if key in st.secrets:
        return st.secrets[key]
    return os.getenv(env_name, default)


AIRTABLE_API_KEY = get_config_value("airtable_api_key", "AIRTABLE_API_KEY", "your_api_key")
BASE_ID = get_config_value("airtable_base_id", "AIRTABLE_BASE_ID", "your_base_id")
TABLE_NAME = get_config_value("airtable_table_name", "AIRTABLE_TABLE_NAME", "your_table_name")

table = Table(AIRTABLE_API_KEY, BASE_ID, TABLE_NAME)

def fetch_data():
    # Fetching all records from Airtable
    data = table.all()
    # Flattening the nested dictionary structure from Airtable
    rows = [record['fields'] for record in data]
    return pd.DataFrame(rows)

def aggregate_usage(usage_series):
    """
    Takes a series of 'X/Y' strings and returns a combined 'SumX/SumY' string.
    """
    total_num = 0
    total_den = 0
    
    for val in usage_series:
        if pd.isna(val) or '/' not in str(val):
            continue
        try:
            num, den = str(val).split('/')
            total_num += int(num)
            total_den += int(den)
        except ValueError:
            continue # Skip malformed strings
            
    return f"{total_num}/{total_den}"

# --- Streamlit UI ---
st.title("🌍 Global Master Usage Tracker")

# 1. Fetch Data
if 'df' not in st.session_state:
    with st.spinner("Fetching data from Airtable..."):
        try:
            if AIRTABLE_API_KEY == "your_api_key" or BASE_ID == "your_base_id" or TABLE_NAME == "your_table_name":
                st.error("Set your Airtable credentials in Streamlit secrets or environment variables before running the app.")
                st.stop()

            df_raw = fetch_data()
            if df_raw.empty:
                st.warning("Airtable returned no records.")
                st.stop()

            # Convert Period to datetime objects for easy filtering
            df_raw['Period'] = pd.to_datetime(df_raw['Period'], errors='coerce')
            df_raw = df_raw.dropna(subset=['Period'])
            st.session_state.df = df_raw
        except HTTPError as exc:
            st.error("Airtable request failed. Check your API key, base ID, and table name.")
            st.caption(str(exc))
            st.stop()
        except Exception as exc:
            st.error("Unexpected error while loading Airtable data.")
            st.caption(str(exc))
            st.stop()

df = st.session_state.df

# 2. Month Selector
# Create a unique list of Year-Month strings for the dropdown
df['Month_Year'] = df['Period'].dt.strftime('%B %Y')
available_months = sorted(df['Month_Year'].unique(), reverse=True)

selected_month = st.selectbox("Select Month", options=available_months)

# 3. Trigger Button
if st.button("Calculate Usage"):
    # Filter by the selected month
    filtered_df = df[df['Month_Year'] == selected_month]
    
    if filtered_df.empty:
        st.warning(f"No records found for {selected_month}.")
    else:
        # Group by Country and apply our custom addition logic
        # We use a lambda to process the 'Master Usage' column for each country group
        results = filtered_df.groupby('Country')['Master Usage'].apply(aggregate_usage).reset_index()
        
        st.subheader(f"Results for {selected_month}")
        st.table(results)
        
        # Optional: Add a 'Grand Total' for all countries combined
        grand_total = aggregate_usage(filtered_df['Master Usage'])
        st.metric("Total Global Usage", grand_total)
