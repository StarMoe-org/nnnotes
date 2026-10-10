//! `nnnotes._deck`: the chart statistics of the deck model ournotes-sim (`ournotes-deck.chart-stats/3`) on a deck
//! data document held in memory, the charts measured in parallel.

use std::sync::Mutex;
use std::sync::atomic::{AtomicUsize, Ordering};

use ournotes_sim::chartstats::{self, ChartStats, ChartStatsCache, Options, REPLAY_SEEDS};
use ournotes_sim::data::DeckData;
use ournotes_sim::error::Error;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

fn value_error(e: impl std::fmt::Display) -> PyErr {
    PyValueError::new_err(e.to_string())
}

/// The statistics of every chart, in chart order, on `workers` threads.
fn measure(
    data: &DeckData,
    options: &Options,
    workers: usize,
    cache: Option<&ChartStatsCache>,
) -> Result<Vec<ChartStats>, Error> {
    let kinds = chartstats::kinds(&data.master);
    let n = data.charts.len();
    let next = AtomicUsize::new(0);
    let out: Mutex<Vec<Option<Result<ChartStats, Error>>>> = Mutex::new((0..n).map(|_| None).collect());
    std::thread::scope(|s| {
        for _ in 0..workers.clamp(1, n.max(1)) {
            s.spawn(|| {
                loop {
                    let i = next.fetch_add(1, Ordering::Relaxed);
                    if i >= n {
                        break;
                    }
                    let c = &data.charts[i];
                    // as chartstats::document names a chart's domain errors
                    let r = match cache {
                        Some(cache) => chartstats::chart_stats_with_cache(&data.master, c, &kinds, options, cache),
                        None => chartstats::chart_stats_with(&data.master, c, &kinds, options),
                    }
                    .map_err(|e| match e {
                        Error::Domain(m) => Error::Domain(format!("chart {}: {m}", c.score_id)),
                        e => e,
                    });
                    let failed = r.is_err();
                    out.lock().expect("results")[i] = Some(r);
                    if failed {
                        next.store(n, Ordering::Relaxed);
                    }
                }
            });
        }
    });
    // charts are taken in order and a taken chart always completes, so every chart before the first failure has
    // its result: the error returned is the first in chart order, as the command's
    let mut stats = Vec::with_capacity(n);
    for r in out.into_inner().expect("results") {
        stats.push(r.expect("a chart before the first failure has run")?);
    }
    Ok(stats)
}

fn document(
    data: &str,
    seeds: Option<usize>,
    workers: Option<usize>,
    aptitude: bool,
    cache: Option<&ChartStatsCache>,
) -> PyResult<String> {
    let options = Options { replay_seeds: seeds.unwrap_or(REPLAY_SEEDS), aptitude };
    let workers = workers.unwrap_or_else(|| std::thread::available_parallelism().map_or(1, |n| n.get()));
    let mut data = DeckData::from_json(data).map_err(value_error)?;
    let charts = std::mem::take(&mut data.charts);
    let mut doc = chartstats::document_with(&data, &options).map_err(value_error)?;
    data.charts = charts;
    let stats = measure(&data, &options, workers, cache).map_err(value_error)?;
    doc["charts"] = serde_json::to_value(stats).map_err(value_error)?;
    serde_json::to_string(&doc).map_err(value_error)
}

/// chart_stats(data, seeds=None, workers=None, aptitude=True) -> str
///
/// The `ournotes-deck.chart-stats/3` document (JSON text) of a deck data document (`nnnotes.deck-data/1` JSON text).
/// `seeds` controls the replay seed list (default 8); expectations use independent nominal probabilities.
/// `workers` controls chart parallelism. `aptitude` includes single Gekisou skill shapes (default true).
/// Raises ValueError for invalid data or a failed native expectation check.
#[pyfunction]
#[pyo3(signature = (data, seeds=None, workers=None, aptitude=true))]
fn chart_stats(
    py: Python<'_>,
    data: &str,
    seeds: Option<usize>,
    workers: Option<usize>,
    aptitude: bool,
) -> PyResult<String> {
    py.detach(|| document(data, seeds, workers, aptitude, None))
}

/// chart_stats_cached(data, seeds=None, workers=None, aptitude=True, *, cache_dir) -> (str, dict)
///
/// Persist exact native computation units and return operation counters separately from the document.
/// Completed units survive a later failure; every call validates the complete input and rebuilds its header.
#[pyfunction]
#[pyo3(signature = (data, seeds=None, workers=None, aptitude=true, *, cache_dir))]
fn chart_stats_cached<'py>(
    py: Python<'py>,
    data: &str,
    seeds: Option<usize>,
    workers: Option<usize>,
    aptitude: bool,
    cache_dir: &str,
) -> PyResult<(String, Bound<'py, PyDict>)> {
    let (text, stats) = py.detach(|| {
        let cache = ChartStatsCache::new(cache_dir).map_err(value_error)?;
        let text = document(data, seeds, workers, aptitude, Some(&cache))?;
        Ok::<_, PyErr>((text, cache.snapshot()))
    })?;
    let counts = PyDict::new(py);
    counts.set_item("requests", stats.requests)?;
    counts.set_item("hits", stats.hits)?;
    counts.set_item("computed", stats.computed)?;
    counts.set_item("writes", stats.writes)?;
    counts.set_item("invalid", stats.invalid)?;
    counts.set_item("bytes", stats.bytes)?;
    Ok((text, counts))
}

/// info() -> dict: the deck model of this module (name: the ournotes-deck repository; version, source and commit:
/// its crate ournotes-sim as built; sourceSha256: the SHA-256 of that crate's sources, equal for builds that run the
/// same model) and the formats it reads and writes.
#[pyfunction]
fn info(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("name", "ournotes-deck")?;
    d.set_item("version", env!("DECK_VERSION"))?;
    d.set_item("source", env!("DECK_SOURCE"))?;
    d.set_item("commit", env!("DECK_COMMIT"))?;
    d.set_item("sourceSha256", ournotes_sim::SOURCE_SHA256)?;
    d.set_item("dataFormat", ournotes_sim::data::FORMAT)?;
    d.set_item("format", chartstats::FORMAT)?;
    Ok(d)
}

#[pymodule]
fn _deck(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(chart_stats, m)?)?;
    m.add_function(wrap_pyfunction!(chart_stats_cached, m)?)?;
    m.add_function(wrap_pyfunction!(info, m)?)?;
    Ok(())
}
