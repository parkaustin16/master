import io
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import streamlit as st
from pyairtable import Api
from requests.exceptions import HTTPError

# --- Configuration & Connection ---
def get_config_value(key, env_name, default=""):
    if key in st.secrets:
        return st.secrets[key]
    return os.getenv(env_name, default)


AIRTABLE_API_KEY  = get_config_value("airtable_api_key",  "AIRTABLE_API_KEY",  "your_api_key")
BASE_ID           = get_config_value("airtable_base_id",   "AIRTABLE_BASE_ID",   "your_base_id")
TABLE_NAME        = get_config_value("airtable_table_name", "AIRTABLE_TABLE_NAME", "your_table_name")
OUTPUT_TABLE_NAME = get_config_value("airtable_output_table_name", "AIRTABLE_OUTPUT_TABLE_NAME", "your_output_table_name")

# Pre-aggregated 2025 data table (on the main base)
TABLE_NAME_2025_PREBUILT = "2025 Master Usage"

OUTPUT_COUNTRY_FIELD  = "country"
OUTPUT_MONTH_FIELD    = "month"
OUTPUT_YEAR_FIELD     = "year"
OUTPUT_USAGE_FIELD    = "Master Usage"
OUTPUT_PLATFORM_FIELD = "platform"
OUTPUT_REGION_FIELD   = "region"
OUTPUT_PCT_FIELD      = "Master Usage (%)"

# Country name → region lookup (case-insensitive)
_REGION_MAP: dict[str, str] = {}
for _region, _countries in {
    "Asia":   ["Australia","Japan","Hong Kong","Taiwan","India","Singapore","Malaysia","Thailand","Vietnam","Philippines","Indonesia"],
    "Europe": ["United Kingdom","Switzerland","France","Germany","Italy","Spain","Netherlands","Czech Republic","Sweden","Portugal","Hungary","Poland","Austria"],
    "LATAM":  ["Mexico","Brazil","Argentina","Chile","Colombia","Peru","Panama"],
    "MEA":    ["Kazakhstan","Turkiye","Egypt","Morocco","Saudi Arabia","South Africa"],
    "Canada": ["Canada"],
}.items():
    for _name in _countries:
        _REGION_MAP[_name.lower()] = _region

def _get_tables():
    """Return (capture_table, output_table, prebuilt_2025_table) using the modern pyairtable Api."""
    api = Api(AIRTABLE_API_KEY)
    return (
        api.table(BASE_ID, TABLE_NAME),
        api.table(BASE_ID, OUTPUT_TABLE_NAME),
        api.table(BASE_ID, TABLE_NAME_2025_PREBUILT),
    )


def _fetch_table_rows(base_id, table_name):
    api = Api(AIRTABLE_API_KEY)
    rows = []
    for r in api.table(base_id, table_name).all():
        normalized = {
            (k if k == 'Master Usage' else k.lower()): v
            for k, v in r['fields'].items()
        }
        rows.append(normalized)
    return rows


def fetch_data():
    """Fetch capture records (2026+) in parallel with the pre-built 2025 table."""
    with ThreadPoolExecutor(max_workers=2) as executor:
        f_capture = executor.submit(_fetch_table_rows, BASE_ID, TABLE_NAME)
        f_2025    = executor.submit(_fetch_table_rows, BASE_ID, TABLE_NAME_2025_PREBUILT)
        capture_rows = f_capture.result()
        prebuilt_rows = f_2025.result()
    return pd.DataFrame(capture_rows), pd.DataFrame(prebuilt_rows)


def upsert_monthly_results(month_label, year_label, results_df):
    _, output_table, _ = _get_tables()
    existing_records = output_table.all()
    existing_map = {}

    for rec in existing_records:
        fields = rec.get('fields', {})
        key = (
            str(fields.get(OUTPUT_COUNTRY_FIELD, '')).strip(),
            str(fields.get(OUTPUT_MONTH_FIELD, '')).strip(),
            str(fields.get(OUTPUT_YEAR_FIELD, '')).strip(),
            str(fields.get(OUTPUT_PLATFORM_FIELD, '')).strip(),
        )
        if key[0] and key[1]:
            existing_map[key] = rec['id']

    created_count = 0
    updated_count = 0

    for _, row in results_df.iterrows():
        country = str(row['country']).strip()
        usage = str(row['Master Usage']).strip()
        platform = str(row.get('platform', '')).strip()
        region = _REGION_MAP.get(country.lower(), '')
        pct = usage_to_pct(usage)

        record_fields = {
            OUTPUT_USAGE_FIELD: usage,
            OUTPUT_PCT_FIELD: pct,
            OUTPUT_COUNTRY_FIELD: country,
            OUTPUT_MONTH_FIELD: month_label,
            OUTPUT_YEAR_FIELD: year_label,
            OUTPUT_PLATFORM_FIELD: platform,
        }
        if region:
            record_fields[OUTPUT_REGION_FIELD] = region
        record_key = (country, month_label, year_label, platform)

        if record_key in existing_map:
            output_table.update(existing_map[record_key], record_fields)
            updated_count += 1
        else:
            output_table.create(record_fields)
            created_count += 1

    return created_count, updated_count

def usage_to_pct(usage_str):
    """Convert 'X/Y' to 'P%', or '' if denominator is 0."""
    try:
        parts = str(usage_str).replace(' ', '').split('/')
        num, den = int(parts[0]), int(parts[1])
        if den == 0:
            return ''
        return f"{round(num / den * 100)}%"
    except (ValueError, IndexError, ZeroDivisionError):
        return ''

REQUIRED_SOURCE_COLS = {'period', 'Master Usage'}



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
    """Fetch capture records and pre-built 2025 records."""
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

    df_capture, df_2025 = fetch_data()

    # Process capture table (2026+ data)
    if df_capture.empty:
        st.warning("Airtable returned no records from the Capture table.")
        st.stop()

    missing = REQUIRED_SOURCE_COLS - set(df_capture.columns)
    if missing:
        st.error(f"Required column(s) not found in the Capture table: {missing}.")
        st.stop()

    df_capture['period'] = pd.to_datetime(df_capture['period'], errors='coerce')
    df_capture = df_capture.dropna(subset=['period'])
    df_capture['_month'] = df_capture['period'].dt.strftime('%B')
    df_capture['_year']  = df_capture['period'].dt.strftime('%Y')
    df_capture['Month_Year'] = df_capture['_month'] + ' ' + df_capture['_year']

    # Process pre-built 2025 table — data already has month/year/Master Usage columns
    if not df_2025.empty:
        # Normalise month/year columns if they exist
        for col_alias in ('month', 'year'):
            if col_alias not in df_2025.columns:
                df_2025[col_alias] = ''
        df_2025['Month_Year'] = df_2025['month'].str.strip() + ' ' + df_2025['year'].astype(str).str.strip()
        df_2025['_month'] = df_2025['month'].str.strip()
        df_2025['_year']  = df_2025['year'].astype(str).str.strip()
        df_2025['_prebuilt'] = True

    df_capture['_prebuilt'] = False
    df_combined = pd.concat([df_capture, df_2025], ignore_index=True)
    return df_combined, df_2025


# --- Streamlit UI ---
st.title("🌍 Global Master Usage Tracker")

# 1. Fetch / Reload Data
col_title, col_reload = st.columns([4, 1])
with col_reload:
    if st.button("🔄 Reload Data"):
        st.session_state.pop('df', None)
        st.session_state.pop('df_2025', None)
        st.session_state.pop('results', None)

if 'df' not in st.session_state:
    with st.spinner("Fetching data from Airtable…"):
        try:
            st.session_state.df, st.session_state.df_2025 = load_data()
        except HTTPError as exc:
            st.error("Airtable request failed. Check your API key, base ID, and table name.")
            st.caption(str(exc))
            st.stop()
        except Exception as exc:
            st.error("Unexpected error while loading Airtable data.")
            st.caption(str(exc))
            st.stop()

df      = st.session_state.df
df_2025 = st.session_state.df_2025

# 2. Month Selector
# Months from capture table (2026+ only) that have valid Master Usage
valid_mask = df['Master Usage'].apply(
    lambda v: pd.notna(v) and '/' in str(v) and str(v).replace(' ', '') != '0/0'
)
capture_months = set(
    df.loc[valid_mask & ~df['_prebuilt'] & (df['_year'] != '2025'), 'Month_Year'].unique()
)

# Months from the pre-built 2025 table
prebuilt_months = set(df_2025['Month_Year'].unique()) if not df_2025.empty else set()

months_with_data = capture_months | prebuilt_months

# Only list months that have valid records
all_month_years = sorted(months_with_data, key=lambda m: pd.to_datetime(m, format='%B %Y'))

selected_month_year = st.selectbox("Select Month", options=all_month_years)
selected_month = selected_month_year.split(' ')[0]
selected_year  = selected_month_year.split(' ')[1]

is_prebuilt = selected_year == '2025'

# 3. Preview
if is_prebuilt:
    filtered_df = df_2025[df_2025['Month_Year'] == selected_month_year]
    st.caption(f"{len(filtered_df)} pre-aggregated record(s) found for {selected_month_year}.")
else:
    filtered_df = df[df['Month_Year'] == selected_month_year]
    st.caption(f"{len(filtered_df)} record(s) found for {selected_month_year}.")

# 4. Calculate
btn_label = "Load Results" if is_prebuilt else "Calculate"
if st.button(btn_label):
    if filtered_df.empty:
        st.warning(f"No records found for {selected_month_year}.")
    elif is_prebuilt:
        # Pass through pre-aggregated data directly
        results = filtered_df.copy()
        for col in ('country', 'platform', 'region', 'month', 'year'):
            if col not in results.columns:
                results[col] = ''
        results['month'] = selected_month
        results['year']  = selected_year
        if 'region' not in results.columns or results['region'].eq('').all():
            results['region'] = results['country'].str.lower().map(_REGION_MAP).fillna('')
        results['Master Usage (%)'] = results['Master Usage'].apply(usage_to_pct)
        results = results[['Master Usage', 'Master Usage (%)', 'country', 'month', 'year', 'platform', 'region']]
        results = results[results['Master Usage'].str.replace(' ', '') != '0/0']

        if results.empty:
            st.warning("No valid Master Usage values found for this month.")
        else:
            st.session_state['results'] = results
            st.session_state['filtered_df'] = filtered_df
            st.session_state['selected_month'] = selected_month
            st.session_state['selected_year'] = selected_year
            st.session_state['selected_month_year'] = selected_month_year
    else:
        # Aggregate from raw capture records
        group_cols = [c for c in ['country', 'platform'] if c in filtered_df.columns]

        if not group_cols:
            total = aggregate_usage(filtered_df['Master Usage'])
            results = pd.DataFrame([{'Master Usage': total}])
        else:
            results = (
                filtered_df
                .groupby(group_cols, dropna=False)['Master Usage']
                .apply(aggregate_usage)
                .reset_index()
            )

        for col in ('country', 'platform'):
            if col not in results.columns:
                results[col] = ''

        results['month'] = selected_month
        results['year'] = selected_year
        results['region'] = results['country'].str.lower().map(_REGION_MAP).fillna('')
        results['Master Usage (%)'] = results['Master Usage'].apply(usage_to_pct)
        results = results[['Master Usage', 'Master Usage (%)', 'country', 'month', 'year', 'platform', 'region']]
        results = results[results['Master Usage'].str.replace(' ', '') != '0/0']

        if results.empty:
            st.warning("All records for this month are missing Master Usage values — nothing to push.")
        else:
            st.session_state['results'] = results
            st.session_state['filtered_df'] = filtered_df
            st.session_state['selected_month'] = selected_month
            st.session_state['selected_year'] = selected_year
            st.session_state['selected_month_year'] = selected_month_year

if 'results' in st.session_state:
    results = st.session_state['results']
    filtered_df = st.session_state['filtered_df']
    selected_month = st.session_state['selected_month']
    selected_year = st.session_state['selected_year']
    selected_month_year = st.session_state['selected_month_year']

    st.subheader(f"Results for {selected_month_year}")
    st.table(results)

    # Regional grand totals
    st.subheader("Regional Totals")
    region_order = ["Asia", "Europe", "LATAM", "MEA", "Canada"]
    region_cols = st.columns(len(region_order))
    for col, region_name in zip(region_cols, region_order):
        region_rows = results[results['region'] == region_name]['Master Usage']
        fraction = aggregate_usage(region_rows) if not region_rows.empty else "0/0"
        pct = usage_to_pct(fraction)
        col.metric(region_name, fraction)
        col.markdown(f"<div style='font-size:1.75rem;font-weight:600;margin-top:-1rem'>{pct if pct else '—'}</div>", unsafe_allow_html=True)

    grand_total = aggregate_usage(results['Master Usage'])
    grand_pct = usage_to_pct(grand_total)
    st.metric("Overall Grand Total", grand_total)
    st.markdown(f"<div style='font-size:1.75rem;font-weight:600;margin-top:-1rem'>{grand_pct if grand_pct else '—'}</div>", unsafe_allow_html=True)

    # --- Excel Export ---
    region_order = ["Asia", "Europe", "LATAM", "MEA", "Canada"]
    totals_rows = []
    for region_name in region_order:
        region_rows = results[results['region'] == region_name]['Master Usage']
        fraction = aggregate_usage(region_rows) if not region_rows.empty else "0/0"
        totals_rows.append({'Region': region_name, 'Master Usage': fraction, 'Master Usage (%)': usage_to_pct(fraction) or '—'})
    totals_rows.append({'Region': 'Overall', 'Master Usage': grand_total, 'Master Usage (%)': grand_pct or '—'})
    df_totals = pd.DataFrame(totals_rows)

    excel_buf = io.BytesIO()
    with pd.ExcelWriter(excel_buf, engine='openpyxl') as writer:
        results.to_excel(writer, sheet_name='Results', index=False)
        df_totals.to_excel(writer, sheet_name='Totals', index=False)
    excel_buf.seek(0)

    st.download_button(
        label="⬇️ Export to Excel",
        data=excel_buf,
        file_name=f"master_usage_{selected_month_year.replace(' ', '_')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    if st.button("Upload to Airtable"):
        try:
            total_rows = len(results)
            progress_bar = st.progress(0, text="Uploading…")
            created_count, updated_count = 0, 0

            _, output_table, _ = _get_tables()
            existing_records = output_table.all()
            existing_map = {}
            for rec in existing_records:
                fields = rec.get('fields', {})
                key = (
                    str(fields.get(OUTPUT_COUNTRY_FIELD, '')).strip(),
                    str(fields.get(OUTPUT_MONTH_FIELD, '')).strip(),
                    str(fields.get(OUTPUT_YEAR_FIELD, '')).strip(),
                    str(fields.get(OUTPUT_PLATFORM_FIELD, '')).strip(),
                )
                if key[0] and key[1]:
                    existing_map[key] = rec['id']

            for i, (_, row) in enumerate(results.iterrows(), start=1):
                country  = str(row['country']).strip()
                usage    = str(row['Master Usage']).strip()
                platform = str(row.get('platform', '')).strip()
                region   = _REGION_MAP.get(country.lower(), '')
                pct      = usage_to_pct(usage)

                record_fields = {
                    OUTPUT_USAGE_FIELD:    usage,
                    OUTPUT_PCT_FIELD:      pct,
                    OUTPUT_COUNTRY_FIELD:  country,
                    OUTPUT_MONTH_FIELD:    selected_month,
                    OUTPUT_YEAR_FIELD:     selected_year,
                    OUTPUT_PLATFORM_FIELD: platform,
                }
                if region:
                    record_fields[OUTPUT_REGION_FIELD] = region

                record_key = (country, selected_month, selected_year, platform)
                if record_key in existing_map:
                    output_table.update(existing_map[record_key], record_fields)
                    updated_count += 1
                else:
                    output_table.create(record_fields)
                    created_count += 1

                progress_bar.progress(i / total_rows, text=f"Uploading… {i}/{total_rows}")

            progress_bar.empty()
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
