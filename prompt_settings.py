"""Resolve prompt sources while preserving saved text and legacy empty values."""

from hashlib import sha256


# Released schema defaults with source commits. Keep these when defaults change.
KNOWN_DEFAULT_HASHES = {
    "usage_prompt": frozenset({
        "d35ea3248bda086ad1cbd67a04a75be98d78600c1768119a914bda6f85f29f41",  # 80b13126
        "a93b222fc8eaaf412ab289aa9c3b4e11e69b350cc6f1547b0fa03ac09a8cf809",  # 05d89677
        "655efd8853900ca100ac099d2809c72a5be83fd61f98a054b532708278d7953f",  # b61d49b1
        "6de6ff188a5525a5a161fa2f43b0ad9083ad116ec6580046296a16cc59c2b88a",  # e0bae54f
        "9f8760be300fd414b71f64e702ba966545dfd851d634ef29b687177742ca4891",  # f88a5300
    }),
    "autonomous_usage_prompt": frozenset({
        "30e2ecbe8300a80a15fa6a78bfe74b57240a25f0add6cb5aac9c62925750c5df",  # 80b13126
    }),
}


def resolve_prompt(config: dict, key: str, default: str) -> str:
    """Auto recognizes shipped defaults; explicit custom always uses saved text."""
    source = config.get(f"{key}_source", "auto")
    if source == "default":
        return default.strip()
    if source == "custom":
        return str(config.get(key) or "").strip()

    if key not in config:
        return default.strip()
    saved = str(config[key] or "").strip()
    # Unknown source values preserve the saved text rather than replacing it.
    if source != "auto":
        return saved
    if not saved:
        return default.strip() if key == "autonomous_usage_prompt" else ""
    normalized = saved.replace("\r\n", "\n")
    digest = sha256(normalized.encode("utf-8")).hexdigest()
    if normalized == default.strip().replace("\r\n", "\n") or digest in KNOWN_DEFAULT_HASHES.get(key, ()):
        return default.strip()
    return saved
