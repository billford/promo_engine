import anthropic

from config import CLAUDE_MODEL


VOICE_CONTEXT = """\
The author is a semi-retired cybersecurity professional and tech writer. Voice characteristics:
- Conversational, self-deprecating, flat delivery of strong opinions
- Never preachy, skeptic-leaning, uses pop culture as entry points
- Dry humor that doesn't announce itself
- Personal stakes stated upfront
- Sounds like a person, not a content calendar

These posts are tagged as AI-assisted. Write as if the author asked you to draft something in their voice.
"""

LINKEDIN_PROMPT_TEMPLATE = """\
Write a LinkedIn post promoting this piece of content.

Content title: {title}
Source: {source}
Description: {description}

Requirements:
- One short paragraph. 40–60 words. That's it.
- First person. State clearly what the article is about and why it's worth reading.
- Lead with the core idea or question the piece addresses — not a vague hook.
- No link in the post body — it will go in the first comment.
- End with 3–5 relevant hashtags drawn from the content's actual topic.
- Final line, exactly as written: [Post written by AI Promotion Engine — article is all human]
- Output the post text only, no surrounding explanation.
"""

BLUESKY_DISCLAIMER = "[Post written by AI Promotion Engine — article is all human]"

BLUESKY_PROMPT_TEMPLATE = """\
Write a Bluesky post promoting this piece of content.

Content title: {title}
Source: {source}
URL: {url}
Description: {description}

Requirements:
- One or two plain sentences. Conversational, not performative.
- Say what the piece is actually about and include the link.
- End with 2–3 relevant hashtags from the content's topic, plus #AIPromoted.
- Final line, exactly as written: [Post written by AI Promotion Engine — article is all human]
- The entire post including the URL and disclaimer must be under 280 characters total.
- Keep the body text short to leave room for the URL and disclaimer.
- Output the post text only, no surrounding explanation.
"""


def _enforce_bluesky_limit(text: str, url: str) -> str:
    if len(text) <= 280:
        return text

    url_pos = text.find(url)
    if url_pos == -1:
        return text[:277] + "..."

    suffix = text[url_pos:]
    available = 280 - len(suffix) - 1  # -1 for the space before suffix
    if available <= 3:
        return suffix[:280]

    truncated = text[:available - 3].rstrip() + "..."
    return truncated + " " + suffix


def write_posts(content: dict, config: dict) -> dict:
    client = anthropic.Anthropic(api_key=config["anthropic_api_key"])

    shared_system = [
        {
            "type": "text",
            "text": VOICE_CONTEXT,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    description = content.get("description", "")[:400]

    def call_claude(prompt: str) -> str:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=shared_system,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()

    linkedin_prompt = LINKEDIN_PROMPT_TEMPLATE.format(
        title=content["title"],
        source=content["source"],
        description=description,
    )
    linkedin_post = call_claude(linkedin_prompt)

    bluesky_prompt = BLUESKY_PROMPT_TEMPLATE.format(
        title=content["title"],
        source=content["source"],
        url=content["url"],
        description=description,
    )
    bluesky_post = call_claude(bluesky_prompt)

    # Enforce Bluesky character limit in code
    bluesky_post = _enforce_bluesky_limit(bluesky_post, content["url"])

    # Ensure #AIPromoted is present
    if "#AIPromoted" not in bluesky_post:
        candidate = bluesky_post.rstrip() + " #AIPromoted"
        bluesky_post = _enforce_bluesky_limit(candidate, content["url"])

    # Ensure disclaimer is present
    if BLUESKY_DISCLAIMER not in bluesky_post:
        candidate = bluesky_post.rstrip() + "\n" + BLUESKY_DISCLAIMER
        bluesky_post = _enforce_bluesky_limit(candidate, content["url"])

    return {
        "linkedin": linkedin_post,
        "bluesky": bluesky_post,
    }
