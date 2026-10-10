"""research.py - STUDENT IMPLEMENTS.  The main script.   Guide: GUIDE.md, part 3.

Usage:  python research.py "survey about world model"
Result: reports/<slug>.md   reports/<slug>.sources.json   reports/<slug>.meta.json
"""
import json  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import sys
import time  # noqa: F401
from collections import Counter  # noqa: F401
from pathlib import Path

from agents import FINALIZER_PATH, REPORT_PATH, SOURCES_PATH, VALIDATOR_PATH, WORKDIR, build_lead_agent  # noqa: F401
from model import make_model  # noqa: F401
from sandbox import download, open_sandbox, upload  # noqa: F401

ROOT = Path(__file__).parent
REPORTS = ROOT / "reports"
VALIDATOR_SOURCE = ROOT / "check_citations.py"
FINALIZER_SOURCE = ROOT / "finalize_citations.py"   # provided: uploaded next to your validator


def slugify(topic):
    """Turn a topic into a safe file name: lower case, runs of non-word characters become one "-", max 60 chars,
    never empty (fall back to "topic"). The topic is user input: "../../x" must not escape reports/."""
    if not topic or not topic.strip():
        return "topic"

    # Replace runs of non-word characters with single hyphen
    slug = re.sub(r'[^\w]+', '-', topic.strip())
    # Remove leading/trailing hyphens
    slug = slug.strip('-')
    # Lower case
    slug = slug.lower()
    # Truncate to 60 chars
    if len(slug) > 60:
        slug = slug[:60]

    # Fallback if empty after processing
    if not slug:
        return "topic"

    return slug


def build_prompt(topic):
    """The user message sent to the lead agent."""
    return f"""Please research and write a comprehensive survey report on the topic: {topic}

Follow the workflow outlined in your system prompt to:
1. Plan the research with write_todos
2. Delegate sub-questions to researcher agents
3. Gather sources from at least 3 different source families
4. Write the report body (without the References section)
5. Run the citation finalizer and validator
6. Spot-check a few claims with the citation-checker

The final outputs will be:
- {REPORT_PATH} (the survey report)
- {SOURCES_PATH} (the sources in JSON format)
"""


def summarize(messages, elapsed, model_name):
    """Return {"model", "elapsed_s", "subagent_calls", "tool_calls": {name: count}, "tokens": {"input", "output"}}.

    Walks the lead's messages; counts tool calls by name (subagent_calls = count of "task" calls).
    Adds input/output token counts from usage_metadata when present.
    """
    subagent_calls = 0
    tool_calls = Counter()
    input_tokens = 0
    output_tokens = 0

    for msg in messages:
        # Handle both dict and object message formats
        msg_dict = msg if isinstance(msg, dict) else msg.model_dump() if hasattr(msg, 'model_dump') else {}

        # Count task tool calls as subagent calls
        if isinstance(msg_dict, dict):
            tc = msg_dict.get('tool_calls', [])
            for call in tc:
                if isinstance(call, dict):
                    name = call.get('name', '')
                    tool_calls[name] += 1
                    if name == 'task':
                        subagent_calls += 1

            # Try to get usage metadata
            usage = msg_dict.get('usage_metadata', {})
            if isinstance(usage, dict):
                input_tokens += usage.get('input_tokens', 0)
                output_tokens += usage.get('output_tokens', 0)

    return {
        "model": model_name,
        "elapsed_s": round(elapsed, 1),
        "subagent_calls": subagent_calls,
        "tool_calls": dict(tool_calls),
        "tokens": {
            "input": input_tokens,
            "output": output_tokens
        }
    }


def save_outputs(backend, topic, messages, elapsed, model_name, reports_dir=REPORTS):
    """Download the report from the sandbox and write the three files into reports_dir. Return the report path.

    Downloads report.md and sources.json, validates them, and writes:
    - <slug>.md (the report)
    - <slug>.sources.json (the sources)
    - <slug>.meta.json (metadata)

    Raises RuntimeError if report or sources are missing/invalid.
    """
    # Ensure reports directory exists
    reports_dir.mkdir(parents=True, exist_ok=True)

    # Download files from sandbox
    files = download(backend, [REPORT_PATH, SOURCES_PATH])

    report_bytes = files.get(REPORT_PATH)
    sources_bytes = files.get(SOURCES_PATH)

    # Check for missing report
    if not report_bytes:
        raise RuntimeError(f"Report not found in sandbox at {REPORT_PATH}")

    report_text = report_bytes.decode('utf-8') if isinstance(report_bytes, bytes) else report_bytes

    if not report_text or not report_text.strip():
        raise RuntimeError("Report is empty")

    # Check for missing sources
    if not sources_bytes:
        raise RuntimeError(f"Sources not found in sandbox at {SOURCES_PATH}")

    try:
        sources = json.loads(sources_bytes.decode('utf-8') if isinstance(sources_bytes, bytes) else sources_bytes)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid sources.json: {e}")

    if not sources:
        raise RuntimeError("sources.json is empty")

    # Generate slug
    slug = slugify(topic)

    # Compute source families (normalize to lowercase)
    source_families = sorted(set(s.get('source', '').lower() for s in sources if s.get('source')))

    # Summarize metadata
    meta = summarize(messages, elapsed, model_name)
    meta["topic"] = topic
    meta["n_sources"] = len(sources)
    meta["source_families"] = source_families

    # Write files
    report_path = reports_dir / f"{slug}.md"
    sources_path = reports_dir / f"{slug}.sources.json"
    meta_path = reports_dir / f"{slug}.meta.json"

    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report_text)

    with open(sources_path, 'w', encoding='utf-8') as f:
        json.dump(sources, f, ensure_ascii=False, indent=2)

    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    return report_path


def main(topic):
    """Return the process exit code (0 ok, 1 failed run, 2 no topic)."""
    # Empty topic check
    if not topic or not topic.strip():
        print("Usage: python research.py \"<topic>\"", file=sys.stderr)
        return 2

    topic = topic.strip()

    try:
        # Create model
        model = make_model()
        model_name = os.getenv("LAB_MODEL", "unknown")

        start_time = time.monotonic()

        # Open sandbox (always cleaned up)
        with open_sandbox() as backend:
            # Create directories
            backend.execute(f"mkdir -p {WORKDIR}/research/notes {WORKDIR}/report")

            # Upload validator and finalizer
            upload(backend, {
                VALIDATOR_PATH: VALIDATOR_SOURCE.read_bytes(),
                FINALIZER_PATH: FINALIZER_SOURCE.read_bytes()
            })

            # Build and run lead agent
            agent = build_lead_agent(backend, model)

            result = agent.invoke(
                {"messages": [{"role": "user", "content": build_prompt(topic)}]},
                config={"recursion_limit": 1000}
            )

            elapsed = time.monotonic() - start_time

            # Get messages from result
            messages = result.get("messages", []) if isinstance(result, dict) else []

            # Save outputs
            report_path = save_outputs(backend, topic, messages, elapsed, model_name)

        print(f"Report saved to: {report_path}")
        return 0

    except RuntimeError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(" ".join(sys.argv[1:])))
