# Build the pinned Rust TPR wheel for the target Docker architecture.
FROM rust:1.92-slim-bookworm AS rust-toolchain
FROM python:3.12.2-slim AS tpr-wheel
COPY --from=rust-toolchain /usr/local/cargo /usr/local/cargo
COPY --from=rust-toolchain /usr/local/rustup /usr/local/rustup
ENV CARGO_HOME=/usr/local/cargo \
    RUSTUP_HOME=/usr/local/rustup \
    PATH=/usr/local/cargo/bin:$PATH \
    PCRE2_SYS_STATIC=1 \
    CARGO_BUILD_JOBS=2
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libc6-dev make pkg-config git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN python -m pip wheel --no-cache-dir --no-deps --wheel-dir /tpr-wheels \
    'git+https://github.com/g0ldyy/torrent-parse-rank.git@e21b3e77987c4a51c8168d9d59de9a718a48c990'

# Only the built wheel is copied into the runtime image; Rust never ships.
FROM python:3.12.2-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y gcc && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
COPY --from=tpr-wheel /tpr-wheels /tpr-wheels
RUN python -m pip install --upgrade pip \
 && python -m pip install -r requirements.txt --no-cache-dir \
 && python -m pip install --no-cache-dir /tpr-wheels/torrent_parse_rank-*.whl \
 && python -m pip check \
 && rm -rf /tpr-wheels
COPY . .
# Fail the image build if the RTN-compatible import or preserved prewarm/2-worker path breaks.
RUN python - <<'PY'
from importlib.metadata import PackageNotFoundError, version
from RTN import parse
from utils.filter_results import _rtn_parse_worker, start_rtn_prewarm, shutdown_rtn_prewarm

assert version('torrent-parse-rank') == '0.1.0'
try:
    version('rank-torrent-name')
except PackageNotFoundError:
    pass
else:
    raise AssertionError('Conflicting Python RTN distribution installed')

result = parse('Show.S02E03-E05.1080p.WEB-DL.mkv')
assert result.seasons == [2] and result.episodes == [3, 4, 5], result
handle = start_rtn_prewarm()
assert handle is not None and handle['workers'] == 2, handle
try:
    for future in handle['futures']:
        future.result(timeout=60)
    jobs = list(handle['executor'].map(
        _rtn_parse_worker,
        ['Show.S02E03-E05.1080p.WEB-DL.mkv', 'Show.S03E02.720p.WEB-DL.mkv'],
        chunksize=1,
    ))
    assert jobs[0][1].episodes == [3, 4, 5], jobs[0][1]
    assert jobs[1][1].seasons == [3] and jobs[1][1].episodes == [2], jobs[1][1]
finally:
    shutdown_rtn_prewarm(handle)
print('TPR RTN-API, parse, Pydantic multiprocessing and 2-worker prewarm: OK')
PY
EXPOSE 3000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3000", "--log-level", "critical"]
