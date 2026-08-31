"""Turning text into vectors, through whichever backend the box can run.

Optional in the strongest sense: with no backend at all, search stays
keyword-only and nothing breaks. That is why every call here returns `None`
rather than raising when embeddings are unavailable — a library without them is
the normal case, not a degraded one.

Two backends, chosen the way the STT ones are:

  * `mistral` — one HTTP call per batch. The key is usually already present for
    Voxtral, and sending transcript *text* to a hosted API is a smaller step
    than the audio Voxtral already uploads. Still a step, so `local` and `off`
    always win over the automatic choice.
  * `local` — sentence-transformers, if it happens to be installed. Not a
    dependency: it pulls in torch, which is larger than the rest of this app put
    together and unusable on a 2-core box anyway.

Vectors are normalised here, once, so every search is a plain dot product.
"""
import logging
import math
import os
import struct
from array import array

import httpx

from config import settings

log = logging.getLogger(__name__)

# Overridable so the whole path — batching, retries, packing, search — can be
# exercised against a stand-in server rather than mocked out.
MISTRAL_URL = os.environ.get("MISTRAL_EMBED_URL", "https://api.mistral.ai/v1/embeddings")

# Batch size for the hosted call. Large enough that indexing an episode is a
# couple of requests, small enough to stay inside the payload limit.
BATCH = 48

# Requests are retried once: indexing walks the whole library, and one blip
# should not leave a hole in it.
TIMEOUT_SECONDS = 60

_local_model = None


def engine() -> str:
    return settings.embed_engine


def available() -> bool:
    return engine() != "off"


def model_name() -> str:
    if engine() == "mistral":
        return settings.embed_model
    if engine() == "local":
        return settings.embed_local_model
    return ""


def normalise(vector: list[float]) -> list[float]:
    length = math.sqrt(sum(v * v for v in vector))
    if length <= 0:
        return vector
    return [v / length for v in vector]


def pack(vector: list[float]) -> bytes:
    """float32 little-endian. `array` rather than numpy: the box has neither."""
    return array("f", vector).tobytes()


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def _embed_mistral(texts: list[str]) -> list[list[float]] | None:
    if not settings.mistral_api_key:
        return None
    out: list[list[float]] = []
    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        for i in range(0, len(texts), BATCH):
            chunk = texts[i:i + BATCH]
            for attempt in (1, 2):
                try:
                    r = client.post(
                        MISTRAL_URL,
                        headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                        json={"model": settings.embed_model, "input": chunk},
                    )
                    r.raise_for_status()
                    data = r.json().get("data", [])
                    if len(data) != len(chunk):
                        raise ValueError(f"asked for {len(chunk)} vectors, got {len(data)}")
                    out.extend(normalise(d["embedding"]) for d in data)
                    break
                except Exception as exc:
                    if attempt == 2:
                        log.warning("embedding batch failed: %s", exc)
                        return None
    return out


def _embed_local(texts: list[str]) -> list[list[float]] | None:
    global _local_model
    try:
        if _local_model is None:
            from sentence_transformers import SentenceTransformer
            _local_model = SentenceTransformer(settings.embed_local_model)
        vectors = _local_model.encode(texts, batch_size=16, show_progress_bar=False)
        return [normalise([float(v) for v in vec]) for vec in vectors]
    except Exception as exc:
        log.warning("local embedding failed: %s", exc)
        return None


def embed(texts: list[str]) -> list[list[float]] | None:
    """Normalised vectors for `texts`, or None if they cannot be produced.

    Blocking — callers run it in an executor. A partial result is never
    returned: a half-indexed episode would look complete and quietly answer
    worse than an unindexed one.
    """
    texts = [t for t in texts if t and t.strip()]
    if not texts or not available():
        return None
    if engine() == "mistral":
        return _embed_mistral(texts)
    return _embed_local(texts)


def similarity(query: list[float], vectors: list[list[float]]) -> list[float]:
    """Cosine similarity against pre-normalised vectors — a dot product.

    Uses numpy when it is installed, which is roughly a hundred times faster on
    a few thousand windows, and falls back to plain Python so the feature works
    on a box without it. That fallback is not theoretical: the production venv
    is deliberately slim.
    """
    if not vectors:
        return []
    try:
        import numpy as np
        matrix = np.asarray(vectors, dtype="float32")
        q = np.asarray(query, dtype="float32")
        return (matrix @ q).tolist()
    except ImportError:
        return [sum(a * b for a, b in zip(vec, query)) for vec in vectors]


def similarity_from_blobs(query: list[float], blobs: list[bytes]) -> list[float]:
    """The same, straight from stored bytes.

    Unpacking first costs more than the arithmetic does: a 1024-dimension vector
    becomes a list of 1024 Python floats at ~8KB a row, so a few thousand windows
    turn 12MB of blobs into hundreds of megabytes of objects on every question.
    numpy reads the buffer as it stands, and the pure-Python path unpacks one row
    at a time so only one is ever resident.
    """
    if not blobs:
        return []
    width = len(query)
    try:
        import numpy as np
        matrix = np.frombuffer(b"".join(blobs), dtype="<f4").reshape(len(blobs), width)
        return (matrix @ np.asarray(query, dtype="float32")).tolist()
    except ImportError:
        out = []
        for blob in blobs:
            vec = unpack(blob)
            out.append(sum(a * b for a, b in zip(vec, query)))
        return out
