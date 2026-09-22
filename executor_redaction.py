"""Pure text sanitization shared by read-only Executor diagnostics."""

import json
import re


_SENSITIVE_KEY = re.compile(
    r"(?i)account|user|token|password|secret|credential|balance|cash|fund|"
    r"price|amount|margin|equity|conid"
)
_OCC_OPTION_SYMBOL = re.compile(r"(?i)\b[A-Z]{1,6}\s*\d{6}[CP]\d{8}\b")
_HUMAN_OPTION_DESCRIPTION = re.compile(
    r"(?i)\b[A-Z]{1,6}\s*(?:\([A-Z0-9]+\)\s*)?"
    r"(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+"
    r"\d{1,2}\s+'?\d{2,4}\s+\d+(?:\.\d+)?\s+(?:CALL|PUT)\b"
)


def safe_error_text(value):
    """Retain useful error wording while removing likely identities and values."""
    if not isinstance(value, str):
        return "[NON_TEXT_ERROR]"
    if value.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            pass
        else:
            def scrub(item):
                if isinstance(item, dict):
                    return {key: "[REDACTED]" if _SENSITIVE_KEY.search(str(key))
                            else scrub(entry) for key, entry in item.items()}
                if isinstance(item, list):
                    return [scrub(entry) for entry in item]
                return item
            value = json.dumps(scrub(parsed), ensure_ascii=True, separators=(",", ":"))
    text = value.replace("\r", " ").replace("\n", " ").replace("\x00", " ")
    text = _OCC_OPTION_SYMBOL.sub("[REDACTED_OPTION_CONTRACT]", text)
    text = _HUMAN_OPTION_DESCRIPTION.sub("[REDACTED_OPTION_CONTRACT]", text)
    text = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
                  "[EMAIL]", text)
    text = re.sub(r"(?i)\b(?:DU|U|D|F)\d{5,}\b", "[ACCOUNT_ID]", text)
    text = re.sub(r"\b\d{6,}\b", "[IDENTIFIER]", text)
    text = re.sub(
        r"(?i)\b(account|username|user|token|password|secret|credential)\s*[:=]\s*[^\s,;{}]+",
        lambda match: match.group(1) + "=[REDACTED]", text)
    text = re.sub(
        r"(?i)\b(balance|cash|funds|buying power|price|amount|margin|equity)\s*[:=]\s*[$€£]?[+-]?\d[\d,.]*",
        lambda match: match.group(1) + "=[REDACTED]", text)
    text = re.sub(
        r"(?i)\b(conid|strike|expiration|expiry|lastTradeDateOrContractMonth)\s*[:=]\s*[^\s,;{}]+",
        lambda match: match.group(1) + "=[REDACTED]", text)
    return re.sub(r"[$€£]\s*[+-]?\d[\d,.]*", "[FINANCIAL_VALUE]", text)
