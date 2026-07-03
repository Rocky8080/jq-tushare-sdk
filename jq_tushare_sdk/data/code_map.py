def to_tushare_code(code: str) -> str:
    raw = str(code).strip()
    if raw.endswith(".XSHE"):
        return raw.replace(".XSHE", ".SZ")
    if raw.endswith(".XSHG"):
        return raw.replace(".XSHG", ".SH")
    if raw.endswith(".SZ") or raw.endswith(".SH"):
        return raw
    if raw.startswith(("0", "3")):
        return f"{raw}.SZ"
    if raw.startswith("6"):
        return f"{raw}.SH"
    return raw


def to_joinquant_code(code: str) -> str:
    raw = str(code).strip()
    if raw.endswith(".SZ"):
        return raw.replace(".SZ", ".XSHE")
    if raw.endswith(".SH"):
        return raw.replace(".SH", ".XSHG")
    if raw.endswith(".XSHE") or raw.endswith(".XSHG"):
        return raw
    if raw.startswith(("0", "3")):
        return f"{raw}.XSHE"
    if raw.startswith("6"):
        return f"{raw}.XSHG"
    return raw


def normalize_date(value) -> str:
    text = str(value)[:10].replace("-", "")
    return text


def joinquant_date(value: str) -> str:
    text = normalize_date(value)
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
