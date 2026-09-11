import re, html
from bs4 import BeautifulSoup
import logging
logger = logging.getLogger(__name__)

def clean_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script","style","header","footer","nav","noscript","iframe"]):
        tag.decompose()
    # remove comments
    text = soup.get_text(separator="\n")
    text = html.unescape(text)
    text = text.replace("\xa0"," ").replace("\u200b","").replace("\ufeff","")
    # strip Word junk but keep content
    text = re.sub(r"mso-[a-z-]+:[^;]+;?", "", text, flags=re.I)
    text = re.sub(r"@page[^\n]+", "", text, flags=re.I)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    # remove MicrosoftInternetExplorer4 markers
    text = text.replace("MicrosoftInternetExplorer4","")
    text = text.replace("User Normal User","")
    return text.strip()

def extract_summary(text: str, words=500) -> str:
    if not text:
        return ""
    return " ".join(text.split()[:words])

def extract_judge_names(text: str):
    if not text:
        return []
    m = re.search(r"(?:BENCH|CORAM|Hon'ble|Honorable)\s*:?\s*([^\n]{5,200})", text, re.I)
    if m:
        raw = m.group(1)
        parts = re.split(r",|\band\b|\n", raw)
        return [j.strip() for j in parts if len(j.strip())>3][:6]
    return []
