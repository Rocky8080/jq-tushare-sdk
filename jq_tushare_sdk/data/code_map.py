_SH_FUND_PREFIXES = ("510", "511", "512", "513", "515", "516", "517", "518", "560", "561", "562", "563", "588", "589")
_SZ_FUND_PREFIXES = ("159", "160", "161", "162", "163", "164", "165", "166", "167", "168", "169")
_SH_INDEX_CODES = {
    "000001", "000016", "000300", "000688", "000852",
    "000905", "000906", "000985",
}


def to_tushare_code(code: str) -> str:
    raw = str(code).strip()
    if raw == "000985.XSHG":
        return "000985.CSI"
    if raw.endswith(".XSHG") and raw.split(".", 1)[0].startswith("801"):
        return raw.replace(".XSHG", ".SI")
    if raw.endswith(".XSHE"):
        return raw.replace(".XSHE", ".SZ")
    if raw.endswith(".XSHG"):
        return raw.replace(".XSHG", ".SH")
    if raw.endswith((".SZ", ".SH", ".SI", ".CSI")):
        return raw
    if raw.startswith(("0", "3")):
        return f"{raw}.SZ"
    if raw.startswith("6"):
        return f"{raw}.SH"
    return raw


def to_joinquant_code(code: str) -> str:
    raw = str(code).strip()
    if raw == "000985.CSI":
        return "000985.XSHG"
    if raw.endswith(".SI") and raw.split(".", 1)[0].startswith("801"):
        return raw.replace(".SI", ".XSHG")
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


def is_tushare_fund_code(code: str) -> bool:
    raw = to_tushare_code(code)
    if "." not in raw:
        return False
    symbol, exchange = raw.split(".", 1)
    if exchange == "SH":
        return symbol.startswith(_SH_FUND_PREFIXES)
    if exchange == "SZ":
        return symbol.startswith(_SZ_FUND_PREFIXES)
    return False


def is_tushare_index_code(code: str) -> bool:
    raw = to_tushare_code(code)
    if is_tushare_sw_index_code(raw):
        return True
    if "." not in raw:
        return False
    symbol, exchange = raw.split(".", 1)
    if exchange == "CSI" and symbol == "000985":
        return True
    if exchange == "SZ":
        return symbol.startswith("399")
    return exchange == "SH" and symbol in _SH_INDEX_CODES


def is_tushare_sw_index_code(code: str) -> bool:
    raw = to_tushare_code(code)
    if "." not in raw:
        return False
    symbol, exchange = raw.split(".", 1)
    return exchange == "SI" and len(symbol) == 6 and symbol.startswith("801")


def normalize_date(value) -> str:
    text = str(value)[:10].replace("-", "")
    return text


def joinquant_date(value: str) -> str:
    text = normalize_date(value)
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
