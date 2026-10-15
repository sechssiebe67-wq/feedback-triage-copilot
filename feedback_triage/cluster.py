"""Dependency-free thematic clustering.

The pipeline is deliberately transparent and explainable:

1. tokenise and normalise text,
2. build TF-IDF vectors,
3. leader-cluster items by cosine similarity to cluster centroids,
4. compute cohesion/separation and flag uncertain clusters.

No opaque model is involved, so every result can be traced back to the input
quotes that produced it.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Dict, Iterable, List, Sequence, Tuple

from .models import Cluster, FeedbackItem


TOKEN_RE = re.compile(r"[a-z0-9äöüß]+", re.UNICODE)

STOPWORDS = {
    # English
    "a", "an", "the", "and", "or", "but", "if", "then", "than", "so", "to",
    "of", "in", "on", "at", "by", "for", "with", "about", "into", "over",
    "is", "are", "was", "were", "be", "been", "being", "am", "do", "does",
    "did", "have", "has", "had", "i", "you", "he", "she", "it", "we", "they",
    "me", "him", "her", "us", "them", "my", "your", "our", "their", "this",
    "that", "these", "those", "there", "here", "not", "no", "yes", "can",
    "could", "would", "should", "will", "just", "very", "too", "also", "as",
    "from", "out", "up", "down", "when", "where", "which", "who", "what",
    "how", "why", "all", "any", "some", "more", "most", "much", "many",
    "get", "got", "one", "two", "like", "really", "still", "even", "only",
    # German
    "der", "die", "das", "und", "oder", "aber", "wenn", "dann", "als", "so",
    "zu", "von", "in", "auf", "an", "bei", "für", "mit", "über", "ist",
    "sind", "war", "waren", "sein", "bin", "bist", "ich", "du", "er", "sie",
    "es", "wir", "ihr", "mich", "dich", "uns", "euch", "mein", "dein",
    "unser", "euer", "dieser", "diese", "dieses", "hier", "da", "nicht",
    "nein", "ja", "kann", "könnte", "würde", "sollte", "wird", "werden",
    "muss", "auch", "sehr", "nur", "schon", "immer", "noch", "wie", "was",
    "wer", "wo", "wann", "warum", "alle", "manche", "mehr", "viele", "ein",
    "eine", "einen", "einem", "eines", "mit", "ohne", "dass", "ob",
}

MIN_TOKEN_LENGTH = 2


def _stem(token: str) -> str:
    """Very light suffix stripping to let plural/inflected forms match.

    This is deliberately conservative: it never strips below three characters
    and leaves ``-ss`` endings untouched (so "class" stays "class").
    """
    if token.endswith("ies") and len(token) >= 5:
        return token[:-3] + "y"
    for suffix in ("ing", "ed", "s"):
        if token.endswith(suffix) and not token.endswith("ss"):
            stem = token[: -len(suffix)]
            if len(stem) >= 3:
                return stem
    return token


def tokenize(text: str) -> List[str]:
    """Lowercase, split on non-word characters and drop stop words."""
    tokens = [t for t in TOKEN_RE.findall((text or "").lower())]
    return [
        _stem(t)
        for t in tokens
        if len(t) >= MIN_TOKEN_LENGTH and t not in STOPWORDS
    ]


def build_tfidf(documents: Sequence[Sequence[str]]) -> List[Dict[str, float]]:
    """Return L2-normalised TF-IDF vectors for the given tokenised documents."""
    doc_count = len(documents)
    if doc_count == 0:
        return []

    df: Dict[str, int] = {}
    for tokens in documents:
        for term in set(tokens):
            df[term] = df.get(term, 0) + 1

    vectors: List[Dict[str, float]] = []
    for tokens in documents:
        counts: Dict[str, int] = {}
        for term in tokens:
            counts[term] = counts.get(term, 0) + 1
        vector: Dict[str, float] = {}
        for term, count in counts.items():
            idf = math.log((1.0 + doc_count) / (1.0 + df[term])) + 1.0
            vector[term] = count * idf
        vectors.append(_normalise(vector))
    return vectors


def _normalise(vector: Dict[str, float]) -> Dict[str, float]:
    norm = math.sqrt(sum(value * value for value in vector.values()))
    if norm == 0.0:
        return {}
    return {term: value / norm for term, value in vector.items()}


def cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
    """Cosine similarity between two sparse vectors."""
    if not a or not b:
        return 0.0
    # vectors produced by build_tfidf are unit length, but centroids are sums
    dot = 0.0
    if len(a) > len(b):
        a, b = b, a
    for term, value in a.items():
        other = b.get(term)
        if other:
            dot += value * other
    if dot == 0.0:
        return 0.0
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _centroid(vectors: Sequence[Dict[str, float]]) -> Dict[str, float]:
    centroid: Dict[str, float] = {}
    for vector in vectors:
        for term, value in vector.items():
            centroid[term] = centroid.get(term, 0.0) + value
    return centroid


def _cluster_id(item_ids: Iterable[str]) -> str:
    digest = hashlib.sha1("|".join(sorted(item_ids)).encode("utf-8")).hexdigest()
    return "c" + digest[:10]


ACRONYMS = {"sso", "pdf", "csv", "api", "ui", "ux", "saml", "nps", "csat", "id"}


def _label(centroid: Dict[str, float], limit: int = 3) -> str:
    terms = sorted(centroid.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    if not terms:
        return "Unlabeled cluster"
    words = [
        term.upper() if term in ACRONYMS else term.capitalize()
        for term, _ in terms
    ]
    return " / ".join(words)


def cluster_items(
    items: Sequence[FeedbackItem],
    threshold: float = 0.18,
    min_support: int = 2,
) -> List[Cluster]:
    """Cluster items with leader clustering over TF-IDF vectors.

    Each item is assigned to the most similar existing cluster if that
    similarity meets ``threshold``; otherwise it starts a new cluster.
    """
    if not items:
        return []

    documents = [tokenize(item.text) for item in items]
    vectors = build_tfidf(documents)

    members: List[List[int]] = []
    centroids: List[Dict[str, float]] = []

    for index, vector in enumerate(vectors):
        best = -1
        best_sim = 0.0
        for cindex, centroid in enumerate(centroids):
            sim = cosine(vector, centroid)
            if sim > best_sim:
                best_sim = sim
                best = cindex
        if best >= 0 and best_sim >= threshold:
            members[best].append(index)
            centroids[best] = _centroid([vectors[i] for i in members[best]])
        else:
            members.append([index])
            centroids.append(dict(vector))

    built: List[Tuple[Cluster, Dict[str, float]]] = []
    for member_indices in members:
        built.append(
            _build_cluster(items, vectors, member_indices, min_support)
        )
    built.sort(key=lambda pair: len(pair[0].item_ids), reverse=True)
    clusters = [cluster for cluster, _ in built]
    _annotate_separation(built)
    return clusters


def _build_cluster(
    items: Sequence[FeedbackItem],
    vectors: Sequence[Dict[str, float]],
    member_indices: Sequence[int],
    min_support: int,
) -> Tuple[Cluster, Dict[str, float]]:
    member_vectors = [vectors[i] for i in member_indices]
    centroid = _centroid(member_vectors)
    item_ids = [items[i].id for i in member_indices]
    cohesion = _cohesion(member_vectors, centroid)

    cluster = Cluster(
        id=_cluster_id(item_ids),
        label=_label(centroid),
        item_ids=item_ids,
        cohesion=round(cohesion, 4),
        origin="auto",
    )

    if len(item_ids) < min_support:
        cluster.uncertainty.append("few_evidence")
    if len(item_ids) > 1 and cohesion < 0.30:
        cluster.uncertainty.append("low_cohesion")
    return cluster, centroid


def _cohesion(
    member_vectors: Sequence[Dict[str, float]], centroid: Dict[str, float]
) -> float:
    if not member_vectors:
        return 0.0
    if len(member_vectors) == 1:
        return 1.0
    return sum(cosine(vector, centroid) for vector in member_vectors) / len(
        member_vectors
    )


def _annotate_separation(
    built: Sequence[Tuple[Cluster, Dict[str, float]]]
) -> None:
    if len(built) < 2:
        return
    for cluster, mine in built:
        nearest = 0.0
        for other_cluster, other in built:
            if other_cluster.id == cluster.id:
                continue
            sim = cosine(mine, other)
            nearest = max(nearest, sim)
        cluster.separation = round(1.0 - nearest, 4)
        if cluster.separation < 0.35 and "low_separation" not in cluster.uncertainty:
            cluster.uncertainty.append("low_separation")


def representative_quotes(
    item_ids: Sequence[str],
    items_by_id: Dict[str, FeedbackItem],
    vectors: Dict[str, Dict[str, float]],
    limit: int = 3,
) -> List[FeedbackItem]:
    """Return the items closest to the cluster centroid, best first."""
    present = [i for i in item_ids if i in items_by_id and i in vectors]
    if not present:
        return []
    if len(present) == 1:
        return [items_by_id[present[0]]]
    member_vectors = [vectors[i] for i in present]
    centroid = _centroid(member_vectors)
    ranked = sorted(
        present, key=lambda i: cosine(vectors[i], centroid), reverse=True
    )
    return [items_by_id[i] for i in ranked[:limit]]
