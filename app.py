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

# Second base — Jan–Nov 2025 historical data (same token, different base)
BASE_ID_2025      = get_config_value("airtable_base_id_2025",    "AIRTABLE_BASE_ID_2025",    "app30tpmHFEoA4W5e")
TABLE_NAME_2025   = get_config_value("airtable_table_name_2025", "AIRTABLE_TABLE_NAME_2025", "tblhtcDXnqDo3L87L")

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
    """Return (capture_table, output_table) using the modern pyairtable Api."""
    api = Api(AIRTABLE_API_KEY)
    return api.table(BASE_ID, TABLE_NAME), api.table(BASE_ID, OUTPUT_TABLE_NAME)


def _fetch_table_rows(base_id, table_name):
    api = Api(AIRTABLE_API_KEY)
    rows = []
    for r in api.table(base_id, table_name).all():
        # Normalize field names to lowercase except 'Master Usage'
        normalized = {
            (k if k == 'Master Usage' else k.lower()): v
            for k, v in r['fields'].items()
        }
        rows.append(normalized)
    return rows


def fetch_data():
    tasks = {TABLE_NAME: (BASE_ID, TABLE_NAME)}
    if BASE_ID_2025 and BASE_ID_2025 != BASE_ID:
        tasks[TABLE_NAME_2025 + "_2025"] = (BASE_ID_2025, TABLE_NAME_2025)

    rows = []
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = {executor.submit(_fetch_table_rows, base, tbl): key
                   for key, (base, tbl) in tasks.items()}
        for future in as_completed(futures):
            rows.extend(future.result())

    df = pd.DataFrame(rows)
    return df


def upsert_monthly_results(month_label, year_label, results_df):
    _, output_table = _get_tables()
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

    df_raw['period'] = pd.to_datetime(df_raw['period'], errors='coerce')
    df_raw = df_raw.dropna(subset=['period'])
    if df_raw.empty:
        st.warning("No records with a valid period date were found.")
        st.stop()

    df_raw['_month'] = df_raw['period'].dt.strftime('%B')
    df_raw['_year'] = df_raw['period'].dt.strftime('%Y')
    df_raw['Month_Year'] = df_raw['_month'] + ' ' + df_raw['_year']
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

# 2. Month Selector — build a full Jan–Dec list for every year in the data
valid_mask = df['Master Usage'].apply(
    lambda v: pd.notna(v) and '/' in str(v) and str(v).replace(' ', '') != '0/0'
)
months_with_data = set(df.loc[valid_mask, 'Month_Year'].unique())

with st.expander("🔍 Debug: raw data info"):
    st.write(f"Total rows: {len(df)}")
    st.write(f"Columns: {list(df.columns)}")
    st.write(f"Years found: {sorted(df['_year'].unique()) if '_year' in df.columns else 'n/a'}")
    st.write(f"2025 base configured: {bool(BASE_ID_2025 and BASE_ID_2025 not in ('your_base_id', ''))}")
    st.write(f"BASE_ID_2025 value: '{BASE_ID_2025}'")
    st.write(f"Secrets keys found: {list(st.secrets.keys())}")
    # Show full secrets structure (keys only, no values) to catch nested sections
    def _secrets_structure(s, prefix=""):
        result = {}
        for k in s.keys():
            full_key = f"{prefix}.{k}" if prefix else k
            try:
                sub = s[k]
                if hasattr(sub, 'keys'):
                    result.update(_secrets_structure(sub, full_key))
                else:
                    result[full_key] = type(sub).__name__
            except Exception:
                result[full_key] = "?"
        return result
    st.write(f"Full secrets structure: {_secrets_structure(st.secrets)}")
    sample_2025 = df[df['_year'] == '2025']['Master Usage'].dropna().head(10).tolist() if '_year' in df.columns else []
    st.write(f"Sample 2025 Master Usage values: {sample_2025}")
    st.write(f"months_with_data: {sorted(months_with_data)}")

# Only list months that have valid records
all_month_years = sorted(months_with_data, key=lambda m: pd.to_datetime(m, format='%B %Y'))

selected_month_year = st.selectbox("Select Month", options=all_month_years)
selected_month = selected_month_year.split(' ')[0]   # e.g. "March"
selected_year  = selected_month_year.split(' ')[1]   # e.g. "2026"

# 3. Preview matching records
filtered_df = df[df['Month_Year'] == selected_month_year]
st.caption(f"{len(filtered_df)} record(s) found for {selected_month_year}.")

# 4. Calculate & Push
if st.button("Calculate & Push to Master Usage Table"):
    if filtered_df.empty:
        st.warning(f"No records found for {selected_month_year}.")
    else:
        # Group by Country + Platform (include only columns that exist)
        group_cols = [c for c in ['country', 'platform'] if c in filtered_df.columns]

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
        for col in ('country', 'platform'):
            if col not in results.columns:
                results[col] = ''

        results['month'] = selected_month
        results['year'] = selected_year
        results['region'] = results['country'].str.lower().map(_REGION_MAP).fillna('')
        results['Master Usage (%)'] = results['Master Usage'].apply(usage_to_pct)
        results = results[['Master Usage', 'Master Usage (%)', 'country', 'month', 'year', 'platform', 'region']]

        # Drop rows where numerator and denominator both summed to 0
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

    grand_total = aggregate_usage(filtered_df['Master Usage'])
    grand_pct = usage_to_pct(grand_total)
    st.metric("Overall Grand Total", grand_total)
    st.markdown(f"<div style='font-size:1.75rem;font-weight:600;margin-top:-1rem'>{grand_pct if grand_pct else '—'}</div>", unsafe_allow_html=True)

    if st.button("Upload to Airtable"):
        try:
            total_rows = len(results)
            progress_bar = st.progress(0, text="Uploading…")
            created_count, updated_count = 0, 0

            _, output_table = _get_tables()
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
