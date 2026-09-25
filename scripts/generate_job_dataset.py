"""
Stage 2 of data collection: synthetic job-execution records built on the scraped catalogue.

Every job is dispatched to a worker node whose hardware and price are a REAL instance type
scraped in Stage 1 (scrape_instance_catalogue.py). The node columns are copied verbatim from
the scraped page (e.g. "16 GiB", "8 vCPUs", "$0.2040 hourly"), so they still need parsing.
Job and node-state telemetry (payload, CPU/memory state, latency, queue wait, ...) is simulated
because production job logs are confidential. The Bottleneck label comes from a resource-pressure
score that uses the scraped capacity of the node (memory, vCPUs) plus noise.

Realistic data-quality problems are injected on purpose (missing telemetry, sentinel values,
inconsistent category spellings, duplicate log writes) so the preprocessing stage is meaningful.
"""

import numpy as np
import pandas as pd

SCRAPED_PATH = "../data/raw/scraped_instance_catalogue.csv"
OUT_PATH = "../data/cloud_job_execution_dataset.csv"
N_JOBS = 10_000
N_DUPLICATES = 35
SEED = 23101

rng = np.random.default_rng(SEED)


def parse_number(series):
    return pd.to_numeric(series.astype(str).str.replace(r"[^0-9.]", "", regex=True), errors="coerce")


def build_node_pool():
    cat = pd.read_csv(SCRAPED_PATH)
    cat["_mem"] = parse_number(cat["instance_memory"])
    cat["_vcpu"] = parse_number(cat["vcpus"])
    cat["_price"] = parse_number(cat["linux_on_demand"])
    pool = cat[
        cat["compute_family"].isin(["General purpose", "Compute optimized", "Memory optimized", "Storage optimized"])
        & cat["_vcpu"].between(2, 96)
        & (cat["_mem"] <= 768)
        & cat["_price"].notna()
    ].reset_index(drop=True)
    weights = 1 / np.sqrt(pool["_vcpu"])  # smaller nodes are far more common in a worker pool
    return pool, (weights / weights.sum()).to_numpy()


def simulate():
    pool, node_weights = build_node_pool()
    nodes = pool.iloc[rng.choice(len(pool), N_JOBS, p=node_weights)].reset_index(drop=True)
    mem_gib, vcpu, price = nodes["_mem"].to_numpy(), nodes["_vcpu"].to_numpy(), nodes["_price"].to_numpy()

    categories = ["WebScraping", "DocumentProcessing", "ETL", "MLTraining", "Aggregation", "Streaming"]
    job_category = rng.choice(categories, N_JOBS, p=[0.26, 0.20, 0.18, 0.12, 0.14, 0.10])
    region = rng.choice(["us-east", "us-west", "eu-central", "ap-south"], N_JOBS, p=[0.35, 0.25, 0.20, 0.20])
    priority = rng.choice(["Low", "Medium", "High", "Critical"], N_JOBS, p=[0.30, 0.38, 0.22, 0.10])

    # submission time: 90 days, busier during working hours
    day = rng.integers(0, 90, N_JOBS)
    hour_p = np.array([2, 1.5, 1, 1, 1, 1.5, 3, 5, 7, 8, 8, 7.5, 7, 7.5, 8, 7.5, 7, 6, 5, 4, 3.5, 3, 2.5, 2.0])
    hour = rng.choice(24, N_JOBS, p=hour_p / hour_p.sum())
    minute = rng.integers(0, 60, N_JOBS)
    submitted = pd.Timestamp("2026-06-01") + pd.to_timedelta(day, "D") + pd.to_timedelta(hour, "h") + pd.to_timedelta(minute, "m")
    peak = ((hour >= 9) & (hour <= 17)).astype(float)

    cat_payload = pd.Series({"WebScraping": 1.3, "DocumentProcessing": 1.2, "ETL": 1.5,
                             "MLTraining": 1.8, "Aggregation": 0.8, "Streaming": 0.5})
    cat_mem_factor = pd.Series({"WebScraping": 3.0, "DocumentProcessing": 4.0, "ETL": 5.0,
                                "MLTraining": 9.0, "Aggregation": 2.5, "Streaming": 1.5})
    cat_time_factor = pd.Series({"WebScraping": 2.2, "DocumentProcessing": 1.6, "ETL": 1.4,
                                 "MLTraining": 4.0, "Aggregation": 0.9, "Streaming": 0.6})
    payload = np.round(rng.lognormal(3.4, 0.95, N_JOBS) * cat_payload[job_category].to_numpy(), 2)

    exec_mem = rng.choice([512, 1024, 2048, 4096, 8192], N_JOBS, p=[0.15, 0.30, 0.30, 0.17, 0.08])
    exec_mem = np.minimum(exec_mem, (mem_gib * 1024 * 0.5).astype(int))
    replicas = rng.choice([1, 2, 3, 4, 5], N_JOBS, p=[0.18, 0.32, 0.28, 0.14, 0.08])
    retries = rng.choice([0, 1, 2, 3], N_JOBS, p=[0.76, 0.15, 0.07, 0.02])

    cpu = np.clip(rng.normal(50 + 8 * peak, 19, N_JOBS), 2, 99.5)
    mem_avail = np.clip(rng.normal(58 - 0.25 * (cpu - 50), 19, N_JOBS), 3, 97)
    threads = np.clip(rng.poisson(10 + 0.6 * vcpu ** 0.5, N_JOBS) + (payload / 60).astype(int), 1, 128)
    region_lat = pd.Series({"us-east": 0, "us-west": 6, "eu-central": 12, "ap-south": 22})[region].to_numpy()
    broker_lat = np.round(rng.gamma(2.2, 18, N_JOBS) + region_lat, 1)
    queue_wait = np.round(rng.gamma(1.8, 40 + 25 * peak), 1)
    disk_io = np.round(np.clip(rng.normal(40, 16, N_JOBS), 1, None), 2)
    net_io = np.round(np.clip(rng.normal(30, 12, N_JOBS), 0.5, None), 2)
    gc_pause = np.round(rng.gamma(1.6, 12 + 0.35 * (100 - mem_avail)), 1)

    # resource-pressure score driven by the scraped node capacity
    avail_mem_mb = mem_gib * 1024 * mem_avail / 100
    demand_mb = payload * cat_mem_factor[job_category].to_numpy() + exec_mem * 0.6
    mem_pressure = np.log(demand_mb / avail_mem_mb)
    threads_per_vcpu = threads / vcpu
    prio_effect = pd.Series({"Low": 0.15, "Medium": 0.0, "High": -0.15, "Critical": -0.35})[priority].to_numpy()
    cat_effect = pd.Series({"WebScraping": 0.35, "DocumentProcessing": 0.1, "ETL": 0.15,
                            "MLTraining": 0.55, "Aggregation": -0.2, "Streaming": -0.35})[job_category].to_numpy()
    score = (1.05 * mem_pressure + 3.4 * cpu / 100 + 0.55 * np.log1p(threads_per_vcpu)
             + 0.007 * queue_wait + 0.009 * broker_lat + 0.018 * gc_pause
             + 0.45 * retries - 0.22 * replicas + cat_effect + prio_effect + 0.25 * peak
             + rng.normal(0, 0.85, N_JOBS))
    threshold = np.quantile(score, 0.68)
    p = 1 / (1 + np.exp(-(score - threshold) / 0.35))
    bottleneck = rng.random(N_JOBS) < p

    base_time = 25 + payload * cat_time_factor[job_category].to_numpy() / np.sqrt(vcpu) * (1 + cpu / 100)
    slow = np.where(bottleneck, rng.lognormal(np.log(2.6), 0.45, N_JOBS), rng.lognormal(0, 0.22, N_JOBS))
    exec_time = np.round(base_time * slow * (1 + 0.8 * retries * bottleneck), 1)
    job_cost = np.round(exec_time / 3600 * price * replicas, 5)

    df = pd.DataFrame({
        "job_id": [f"JOB-{100000 + i}" for i in range(N_JOBS)],
        "submitted_at": submitted.strftime("%Y-%m-%d %H:%M"),
        "job_category": job_category,
        "priority_level": priority,
        "region": region,
        "payload_size_mb": payload,
        "executor_memory_mb": exec_mem,
        "replica_count": replicas,
        "retry_count": retries,
        "node_instance_type": nodes["api_name"],
        "node_family": nodes["compute_family"],
        "node_vcpus": nodes["vcpus"],
        "node_memory": nodes["instance_memory"],
        "node_network": nodes["network_performance"],
        "node_price_hourly": nodes["linux_on_demand"],
        "node_spot_price_hourly": nodes["linux_spot_min"],
        "cpu_utilization_pct": np.round(cpu, 1),
        "pod_memory_available_pct": np.round(mem_avail, 1),
        "active_thread_count": threads,
        "broker_latency_ms": broker_lat,
        "queue_wait_time_ms": queue_wait,
        "disk_io_mbps": disk_io,
        "network_io_mbps": net_io,
        "gc_pause_time_ms": gc_pause,
        "execution_time_sec": exec_time,
        "job_cost_usd": job_cost,
        "target_label": np.where(bottleneck, "Bottleneck", "Normal"),
    })
    return df


def inject_quality_issues(df):
    df = df.copy()
    variants = {"WebScraping": ["webscraping", "Web Scraping", "web_scraping "],
                "DocumentProcessing": ["Doc Processing", "documentprocessing"],
                "ETL": ["etl", "ETL "], "MLTraining": ["ML Training", "mltraining"],
                "Aggregation": ["aggregation"], "Streaming": ["streaming", "Streaming "]}
    idx = df.sample(frac=0.03, random_state=SEED).index
    df.loc[idx, "job_category"] = [rng.choice(variants[c]) for c in df.loc[idx, "job_category"]]
    idx = df.sample(frac=0.02, random_state=SEED + 1).index
    df.loc[idx, "region"] = df.loc[idx, "region"].str.upper()

    for col, frac, seed in [("broker_latency_ms", 0.015, 11), ("queue_wait_time_ms", 0.02, 12),
                            ("network_io_mbps", 0.025, 13), ("gc_pause_time_ms", 0.01, 14),
                            ("pod_memory_available_pct", 0.008, 15)]:
        df.loc[df.sample(frac=frac, random_state=seed).index, col] = np.nan

    # telemetry agent glitches: impossible CPU readings and a -1 "no reading" sentinel
    idx = df.sample(n=22, random_state=21).index
    df.loc[idx, "cpu_utilization_pct"] = np.round(rng.uniform(101, 180, len(idx)), 1)
    idx = df.sample(n=18, random_state=22).index
    df.loc[idx, "broker_latency_ms"] = -1.0

    dupes = df.sample(n=N_DUPLICATES, random_state=23)
    df = pd.concat([df, dupes], ignore_index=True)
    return df.sample(frac=1, random_state=SEED).reset_index(drop=True)


def main():
    df = inject_quality_issues(simulate())
    df.to_csv(OUT_PATH, index=False)
    print(f"Saved {len(df):,} rows x {df.shape[1]} columns -> {OUT_PATH}")
    print(df["target_label"].value_counts(normalize=True).round(3))
    print("unique scraped instance types used:", df["node_instance_type"].nunique())


if __name__ == "__main__":
    main()
