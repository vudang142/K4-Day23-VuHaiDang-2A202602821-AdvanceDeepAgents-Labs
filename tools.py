"""tools.py - STUDENT IMPLEMENTS.  Source tools for the research agents.   Guide: GUIDE.md, part 1.

Rules for every tool:
  * runs on the HOST (not in the sandbox): API keys must never enter the sandbox;
  * returns a STRING (JSON text of compact records) and NEVER raises:
        "NO RESULTS"  when the source answers with nothing,
        "ERROR: ..."  when the source keeps failing after the retries (the agent then tries another source);
  * the docstring is the tool description the LLM reads: keep it precise (what it does, what it returns, when to use it).
Try your tools without any agent:   python tools.py
"""
import json  # noqa: F401
import os  # noqa: F401
import re
import time  # noqa: F401
import xml.etree.ElementTree  # noqa: F401  (arXiv answers with Atom XML)
import random

import httpx  # noqa: F401
from langchain_core.tools import tool

# ---- constants (given) ----
ARXIV_URL = "https://export.arxiv.org/api/query"  # https only: http answers 301
HF_DAILY_URL = "https://huggingface.co/api/daily_papers"
HF_SEARCH_URL = "https://huggingface.co/api/papers/search"
EXA_URL = "https://mcp.exa.ai/mcp"


class RetryableError(Exception):
    """Given. Raise it inside a call to ask with_retry to wait and try again (retry_after in seconds, optional)."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


# ---- Track last arXiv call time for rate limiting ----
_last_arxiv_call = 0.0


# ---- TODO 1: retry helper ----
def with_retry(fn, *, attempts=5, base=1.0, cap=30.0):
    """Call fn(); when it raises RetryableError, wait and call it again.

    Implements:
      - Exponential backoff with jitter for retryable errors (429, 5xx, TransportError)
      - Respects Retry-After header when present
      - Caps delay at `cap` seconds
      - Does not sleep after the last attempt
    """
    for attempt in range(attempts):
        try:
            return fn()
        except RetryableError as e:
            if attempt == attempts - 1:
                raise

            # Use server's Retry-After if available, otherwise exponential backoff
            if e.retry_after is not None:
                delay = float(e.retry_after)
            else:
                delay = min(base * (2 ** attempt), cap)
                # Add random jitter (±25% of delay)
                jitter = delay * 0.25 * (2 * random.random() - 1)
                delay = delay + jitter

            delay = max(0.1, min(delay, cap))  # Ensure positive and capped
            time.sleep(delay)


def _is_retryable_error(exc):
    """Check if an exception is retryable (429, 5xx, or TransportError)."""
    if isinstance(exc, RetryableError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    if isinstance(exc, httpx.TransportError):
        return True
    return False


def _make_retryable(fn, attempts=5, base=1.0, cap=30.0):
    """Wrapper that converts retryable errors to RetryableError."""
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except RetryableError:
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (429, 500, 502, 503, 504):
                retry_after = None
                if e.response.status_code == 429:
                    retry_after = e.response.headers.get("Retry-After")
                raise RetryableError(str(e), retry_after=retry_after)
            raise
        except httpx.TransportError as e:
            raise RetryableError(str(e))
    return wrapper


# ---- TODO 2: arXiv ----
@tool
def arxiv_search(query: str, max_results: int = 10) -> str:
    """Search arXiv papers by keywords, newest first. Returns a JSON list of {id, url, published, title, summary}."""
    global _last_arxiv_call

    # Clean query: keep only word characters, spaces, hyphens, AND/OR operators
    cleaned_query = re.sub(r'[^\w\s\-]|^(AND|OR|NOT)$', '', query, flags=re.IGNORECASE)
    cleaned_query = re.sub(r'\s+', ' ', cleaned_query).strip()

    if not cleaned_query:
        return "NO RESULTS"

    # Respect arXiv etiquette: at least 3 seconds between calls
    elapsed = time.time() - _last_arxiv_call
    if elapsed < 3.0:
        time.sleep(3.0 - elapsed)

    def _fetch():
        # Build search query terms
        terms = cleaned_query.split()
        search_query = " AND ".join(f"all:{t}" for t in terms if t)

        params = {
            "search_query": search_query,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": min(max(1, max_results), 30)
        }

        response = httpx.get(ARXIV_URL, params=params, timeout=60)

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            raise RetryableError("HTTP 429 rate limited", retry_after=retry_after)
        if response.status_code >= 500:
            raise RetryableError(f"HTTP {response.status_code}")
        if response.status_code == 301:
            raise RetryableError("HTTP 301 - use HTTPS")
        response.raise_for_status()

        return response.text

    try:
        xml_text = with_retry(_make_retryable(_fetch), attempts=5, base=2.0, cap=60.0)
        _last_arxiv_call = time.time()

        # Parse Atom XML
        root = xml.etree.ElementTree.fromstring(xml_text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}

        records = []
        for entry in root.findall("atom:entry", ns):
            id_elem = entry.find("atom:id", ns)
            published_elem = entry.find("atom:published", ns)
            title_elem = entry.find("atom:title", ns)
            summary_elem = entry.find("atom:summary", ns)

            if id_elem is None:
                continue

            # Extract arXiv ID (remove version suffix like v1)
            arxiv_id = id_elem.text
            if "/abs/" in arxiv_id:
                arxiv_id = arxiv_id.split("/abs/")[-1]
                arxiv_id = re.sub(r'v\d+$', '', arxiv_id)  # Remove version

            url = f"https://arxiv.org/abs/{arxiv_id}"

            # Normalize whitespace in title and summary
            title = " ".join(title_elem.text.split()) if title_elem is not None and title_elem.text else "Untitled"
            summary = " ".join(summary_elem.text.split()) if summary_elem is not None and summary_elem.text else ""

            # Truncate summary to ~600 chars
            if len(summary) > 600:
                summary = summary[:597] + "..."

            published = published_elem.text[:10] if published_elem is not None and published_elem.text else ""

            records.append({
                "id": arxiv_id,
                "url": url,
                "published": published,
                "title": title,
                "summary": summary
            })

        if not records:
            return "NO RESULTS"

        return json.dumps(records, ensure_ascii=False)

    except RetryableError:
        return "ERROR: arXiv rate limited after retries"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {str(e)}"


# ---- TODO 3: Hugging Face ----
@tool
def hf_daily_papers(limit: int = 30, date: str = "", keyword: str = "") -> str:
    """Hugging Face Daily Papers = what is trending in AI research. Returns a JSON list of
    {id, url, published, title, summary, upvotes, github, stars} sorted by upvotes. `date` is YYYY-MM-DD (empty = latest).
    `keyword` filters title/summary; there is no topic search on this endpoint (use hf_search_papers for a topic)."""

    def _fetch():
        params = {"limit": min(max(1, limit), 100)}
        if date:
            params["date"] = date

        response = httpx.get(HF_DAILY_URL, params=params, timeout=60)

        if response.status_code >= 500:
            raise RetryableError(f"HTTP {response.status_code}")
        response.raise_for_status()

        return response.json()

    try:
        data = with_retry(_make_retryable(_fetch), attempts=3, base=1.0, cap=30.0)

        records = []
        for item in data:
            paper = item.get("paper", {})
            paper_id = paper.get("id")

            if not paper_id:
                continue

            title = item.get("title") or paper.get("title") or "Untitled"
            summary = item.get("summary") or paper.get("summary") or ""

            # Filter by keyword if provided
            if keyword:
                if keyword.lower() not in (title + " " + summary).lower():
                    continue

            upvotes = item.get("upvotes", 0)
            github_repo = paper.get("githubRepo")
            github_stars = paper.get("githubStars", 0)
            published_at = paper.get("publishedAt", "")[:10] if paper.get("publishedAt") else ""

            records.append({
                "id": paper_id,
                "url": f"https://huggingface.co/papers/{paper_id}",
                "published": published_at,
                "title": title,
                "summary": summary[:600] + "..." if len(summary) > 600 else summary,
                "upvotes": upvotes,
                "github": github_repo or "",
                "stars": github_stars
            })

        # Sort by upvotes descending
        records.sort(key=lambda x: x.get("upvotes", 0), reverse=True)

        if not records:
            return "NO RESULTS"

        return json.dumps(records, ensure_ascii=False)

    except RetryableError:
        return "ERROR: HuggingFace rate limited after retries"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {str(e)}"


@tool
def hf_search_papers(query: str, limit: int = 10) -> str:
    """Search Hugging Face papers by topic. Returns a JSON list of
    {id, url, published, title, summary, upvotes, github, stars}."""

    def _fetch():
        params = {"q": query, "limit": min(max(1, limit), 50)}

        response = httpx.get(HF_SEARCH_URL, params=params, timeout=60)

        if response.status_code >= 500:
            raise RetryableError(f"HTTP {response.status_code}")
        response.raise_for_status()

        return response.json()

    try:
        data = with_retry(_make_retryable(_fetch), attempts=3, base=1.0, cap=30.0)

        records = []
        for item in data:
            paper = item.get("paper", {})
            paper_id = paper.get("id")

            if not paper_id:
                continue

            # Prefer ai_summary over summary
            title = paper.get("title") or item.get("title") or "Untitled"
            summary = paper.get("ai_summary") or paper.get("summary") or item.get("summary") or ""

            upvotes = item.get("upvotes", 0)
            github_repo = paper.get("githubRepo")
            github_stars = paper.get("githubStars", 0)
            published_at = paper.get("publishedAt", "")[:10] if paper.get("publishedAt") else ""

            records.append({
                "id": paper_id,
                "url": f"https://huggingface.co/papers/{paper_id}",
                "published": published_at,
                "title": title,
                "summary": summary[:600] + "..." if len(summary) > 600 else summary,
                "upvotes": upvotes,
                "github": github_repo or "",
                "stars": github_stars
            })

        if not records:
            return "NO RESULTS"

        return json.dumps(records, ensure_ascii=False)

    except RetryableError:
        return "ERROR: HuggingFace rate limited after retries"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {str(e)}"


# ---- TODO 4: web search / fetch through the Exa MCP endpoint ----
@tool
def web_search(query: str, objective: str = "", num_results: int = 5) -> str:
    """Search the web (Exa). Describe the ideal page in natural language. Returns clean text of the top results with URLs."""

    # Build objective from query if empty
    if not objective:
        objective = f"Find pages about {query}"

    # Build Exa URL with API key if available
    exa_url = EXA_URL
    api_key = os.getenv("EXA_API_KEY", "")
    if api_key:
        exa_url = f"{exa_url}?exaApiKey={api_key}"

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "web_search_exa",
            "arguments": {
                "query": query,
                "objective": objective,
                "numResults": min(max(1, num_results), 10)
            }
        }
    }

    def _fetch():
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"
        }

        response = httpx.post(exa_url, json=payload, headers=headers, timeout=120)

        if response.status_code >= 500:
            raise RetryableError(f"HTTP {response.status_code}")
        response.raise_for_status()

        return response.text

    try:
        response_text = with_retry(_make_retryable(_fetch), attempts=3, base=2.0, cap=60.0)

        # Parse SSE response: look for lines starting with "data:"
        lines = response_text.strip().split("\n")
        for line in lines:
            if line.startswith("data:"):
                data_str = line[5:].strip()
                if data_str:
                    try:
                        data = json.loads(data_str)

                        # Check for JSON-RPC error
                        if "error" in data:
                            error_msg = data["error"].get("message", str(data["error"]))
                            # Check if this is a rate limit error
                            if "_meta" in data and data["_meta"].get("ratelimited"):
                                raise RetryableError("Exa rate limited")
                            raise Exception(f"Exa error: {error_msg}")

                        # Check for rate limit flag in _meta
                        if "result" in data:
                            result = data["result"]
                            if isinstance(result, dict) and result.get("_meta", {}).get("ratelimited"):
                                raise RetryableError("Exa rate limited via _meta flag")

                            # Extract text content
                            if "content" in result:
                                texts = []
                                for item in result["content"]:
                                    if isinstance(item, dict) and item.get("type") == "text":
                                        texts.append(item.get("text", ""))
                                    elif isinstance(item, str):
                                        texts.append(item)
                                return "\n\n".join(texts)

                        return str(result)

                    except json.JSONDecodeError:
                        continue

        return "NO RESULTS"

    except RetryableError as e:
        return "ERROR: Exa rate limited after retries"
    except Exception as e:
        # Redact API key from error message
        error_str = str(e)
        if api_key and api_key in error_str:
            error_str = error_str.replace(api_key, "[REDACTED]")
        return f"ERROR: {type(e).__name__}: {error_str}"


@tool
def web_fetch(url: str) -> str:
    """Read the full content of one web page (e.g. an arXiv abstract page) as markdown. Long pages are truncated."""

    # Build Exa URL with API key if available
    exa_url = EXA_URL
    api_key = os.getenv("EXA_API_KEY", "")
    if api_key:
        exa_url = f"{exa_url}?exaApiKey={api_key}"

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "web_fetch_exa",
            "arguments": {
                "urls": [url]
            }
        }
    }

    def _fetch():
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"
        }

        response = httpx.post(exa_url, json=payload, headers=headers, timeout=120)

        if response.status_code >= 500:
            raise RetryableError(f"HTTP {response.status_code}")
        response.raise_for_status()

        return response.text

    try:
        response_text = with_retry(_make_retryable(_fetch), attempts=3, base=2.0, cap=60.0)

        # Parse SSE response
        lines = response_text.strip().split("\n")
        for line in lines:
            if line.startswith("data:"):
                data_str = line[5:].strip()
                if data_str:
                    try:
                        data = json.loads(data_str)

                        # Check for JSON-RPC error
                        if "error" in data:
                            error_msg = data["error"].get("message", str(data["error"]))
                            if "_meta" in data and data["_meta"].get("ratelimited"):
                                raise RetryableError("Exa rate limited")
                            raise Exception(f"Exa error: {error_msg}")

                        if "result" in data:
                            result = data["result"]
                            if isinstance(result, dict) and result.get("_meta", {}).get("ratelimited"):
                                raise RetryableError("Exa rate limited via _meta flag")

                            if "content" in result:
                                texts = []
                                for item in result["content"]:
                                    if isinstance(item, dict) and item.get("type") == "text":
                                        texts.append(item.get("text", ""))
                                    elif isinstance(item, str):
                                        texts.append(item)
                                full_text = "\n\n".join(texts)

                                # Truncate to ~12000 chars
                                if len(full_text) > 12000:
                                    full_text = full_text[:11997] + "..."
                                return full_text

                        return str(result)

                    except json.JSONDecodeError:
                        continue

        return "NO RESULTS"

    except RetryableError as e:
        return "ERROR: Exa rate limited after retries"
    except Exception as e:
        # Redact API key from error message
        error_str = str(e)
        if api_key and api_key in error_str:
            error_str = error_str.replace(api_key, "[REDACTED]")
        return f"ERROR: {type(e).__name__}: {error_str}"


# ---- TODO 5: registry (the researcher subagent gets exactly these) ----
SOURCE_TOOLS = [arxiv_search, hf_daily_papers, hf_search_papers, web_search, web_fetch]


if __name__ == "__main__":
    for name, fn, args in [
        ("arxiv_search", arxiv_search, {"query": "world model", "max_results": 3}),
        ("hf_daily_papers", hf_daily_papers, {"limit": 20}),
        ("hf_search_papers", hf_search_papers, {"query": "world model", "limit": 3}),
        ("web_search", web_search, {"query": "survey paper on world models", "num_results": 2}),
        ("web_fetch", web_fetch, {"url": "https://arxiv.org/abs/1803.10122"}),
    ]:
        try:
            print(f"== {name}\n{fn.invoke(args)[:400]}\n")
        except NotImplementedError as exc:
            print(f"== {name}: not implemented yet ({exc})\n")
