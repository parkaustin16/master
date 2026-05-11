import os

import pandas as pd
import streamlit as st
from pyairtable import Api
from requests.exceptions import HTTPError

# --- Configuration & Connection ---
def get_config_value(key, env_name, default=""):
    if key in st.secrets:
        return st.secrets[key]
    return os.getenv(env_name, default)


AIRTABLE_API_KEY = get_config_value("airtable_api_key", "AIRTABLE_API_KEY", "your_api_key")
BASE_ID = get_config_value("airtable_base_id", "AIRTABLE_BASE_ID", "your_base_id")
TABLE_NAME = get_config_value("airtable_table_name", "AIRTABLE_TABLE_NAME", "your_table_name")
OUTPUT_TABLE_NAME = get_config_value("airtable_output_table_name", "AIRTABLE_OUTPUT_TABLE_NAME", "your_output_table_name")

OUTPUT_COUNTRY_FIELD = get_config_value("airtable_output_country_field", "AIRTABLE_OUTPUT_COUNTRY_FIELD", "Country")
OUTPUT_MONTH_FIELD = get_config_value("airtable_output_month_field", "AIRTABLE_OUTPUT_MONTH_FIELD", "Month")
OUTPUT_USAGE_FIELD = get_config_value("airtable_output_usage_field", "AIRTABLE_OUTPUT_USAGE_FIELD", "Master Usage")
OUTPUT_PLATFORM_FIELD = get_config_value("airtable_output_platform_field", "AIRTABLE_OUTPUT_PLATFORM_FIELD", "Platform")

def _get_tables():
    """Return (capture_table, output_table) using the modern pyairtable Api."""
    api = Api(AIRTABLE_API_KEY)
    return api.table(BASE_ID, TABLE_NAME), api.table(BASE_ID, OUTPUT_TABLE_NAME)


def fetch_data():
    capture_table, _ = _get_tables()
    data = capture_table.all()
    rows = [record['fields'] for record in data]
    return pd.DataFrame(rows)


def upsert_monthly_results(month_label, results_df):
    _, output_table = _get_tables()
    existing_records = output_table.all()
    existing_map = {}

    for rec in existing_records:
        fields = rec.get('fields', {})
        key = (
            str(fields.get(OUTPUT_COUNTRY_FIELD, '')).strip(),
            str(fields.get(OUTPUT_MONTH_FIELD, '')).strip(),
            str(fields.get(OUTPUT_PLATFORM_FIELD, '')).strip(),
        )
        if key[0] and key[1]:
            existing_map[key] = rec['id']

    created_count = 0
    updated_count = 0

    for _, row in results_df.iterrows():
        country = str(row['Country']).strip()
        usage = str(row['Master Usage']).strip()
        platform = str(row.get('Platform', '')).strip()

        record_fields = {
            OUTPUT_USAGE_FIELD: usage,
            OUTPUT_COUNTRY_FIELD: country,
            OUTPUT_MONTH_FIELD: month_label,
            OUTPUT_PLATFORM_FIELD: platform,
        }
        record_key = (country, month_label, platform)

        if record_key in existing_map:
            output_table.update(existing_map[record_key], record_fields)
            updated_count += 1
        else:
            output_table.create(record_fields)
            created_count += 1

    return created_count, updated_count

REQUIRED_SOURCE_COLS = {'Period', 'Master Usage'}

def aggregate_usage(usage_series):
    """
    Takes a Series of 'X/Y' strings and returns a combined 'SumX/SumY' string,
    adding numerators together and denominators together independently.
    e.g. ['2/12', '4/5'] → '6/17'
    """
    total_num = 0
    total_den = 0
    for val in usage_series:
        if pd.isna(val) or '/' not in str(val):
            continue
        try:
            parts = str(val).strip().split('/')
            total_num += int(parts[0].strip())
            total_den += int(parts[1].strip())
        except (ValueError, IndexError):
            continue
    return f"{total_num}/{total_den}"


def load_data():
    """Fetch and pre-process records from the Capture table."""
    if (
        AIRTABLE_API_KEY == "your_api_key"
        or BASE_ID == "your_base_id"
        or TABLE_NAME == "your_table_name"
        or OUTPUT_TABLE_NAME == "your_output_table_name"
    ):
        st.error(
            "Set your Airtable credentials in `.streamlit/secrets.toml` or as "
            "environment variables before running the app."
        )
        st.stop()

    df_raw = fetch_data()
    if df_raw.empty:
        st.warning("Airtable returned no records.")
        st.stop()

    missing = REQUIRED_SOURCE_COLS - set(df_raw.columns)
    if missing:
        st.error(
            f"Required column(s) not found in the Capture table: {missing}. "
            "Check your Airtable field names."
        )
        st.stop()

    df_raw['Period'] = pd.to_datetime(df_raw['Period'], errors='coerce')
    df_raw = df_raw.dropna(subset=['Period'])
    if df_raw.empty:
        st.warning("No records with a valid Period date were found.")
        st.stop()

    df_raw['Month_Year'] = df_raw['Period'].dt.strftime('%B %Y')
    return df_raw


# --- Streamlit UI ---
st.title("🌍 Global Master Usage Tracker")

# 1. Fetch / Reload Data
col_title, col_reload = st.columns([4, 1])
with col_reload:
    if st.button("🔄 Reload Data"):
        st.session_state.pop('df', None)

if 'df' not in st.session_state:
    with st.spinner("Fetching data from Airtable…"):
        try:
            st.session_state.df = load_data()
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
available_months = sorted(df['Month_Year'].unique(), reverse=True)
selected_month = st.selectbox("Select Month", options=available_months)

# 3. Preview matching records
filtered_df = df[df['Month_Year'] == selected_month]
st.caption(f"{len(filtered_df)} record(s) found for {selected_month}.")

# 4. Calculate & Push
if st.button("Calculate & Push to Master Usage Table"):
    if filtered_df.empty:
        st.warning(f"No records found for {selected_month}.")
    else:
        # Group by Country + Platform (include only columns that exist)
        group_cols = [c for c in ['Country', 'Platform'] if c in filtered_df.columns]

        if not group_cols:
            # No grouping dimensions — aggregate everything into one row
            total = aggregate_usage(filtered_df['Master Usage'])
            results = pd.DataFrame([{'Master Usage': total}])
        else:
            results = (
                filtered_df
                .groupby(group_cols, dropna=False)['Master Usage']
                .apply(aggregate_usage)
                .reset_index()
            )

        # Ensure expected columns exist
        for col in ('Country', 'Platform'):
            if col not in results.columns:
                results[col] = ''

        results['Month'] = selected_month
        results = results[['Master Usage', 'Country', 'Month', 'Platform']]

        st.subheader(f"Results for {selected_month}")
        st.table(results)

        # Grand total across all groups
        grand_total = aggregate_usage(filtered_df['Master Usage'])
        st.metric("Grand Total", grand_total)

        try:
            created_count, updated_count = upsert_monthly_results(selected_month, results)
            st.success(
                f"Synced to **{OUTPUT_TABLE_NAME}**: "
                f"{created_count} record(s) created, {updated_count} updated."
            )
        except HTTPError as exc:
            st.error("Failed to write results to the output table. Check field names and permissions.")
            st.caption(str(exc))
        except Exception as exc:
            st.error("Unexpected error while writing to the output table.")
            st.caption(str(exc))
