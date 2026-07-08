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
- Avoid em-dashes (—). Use a comma, period, or restructure the sentence instead.
- Summarize what the piece actually covers. Lead with the substance, not a personal anecdote.
- Do NOT open with "spent X time/years doing Y" or any variation of that construction.
- Do NOT editorialize. No "fascinating," "important," "you should read this," or similar. Let the content speak.
- Vary the sentence structure. Do not default to a fixed opener pattern.
- No link in the post body — it will go in the first comment.
- End with 3–5 relevant hashtags drawn from the content's actual topic.
- Final line, exactly as written: [Post written by AI Promotion Engine — article is all human]
- Output the post text only, no surrounding explanation.
"""


FACEBOOK_PROMPT_TEMPLATE = """\
Write a Facebook post promoting this piece of content.

Content title: {title}
Source: {source}
URL: {url}
Description: {description}

Requirements:
- Casual, conversational. Same energy as a real person sharing something interesting with friends.
- No em-dashes (—). Use a comma, period, or just cut the clause instead.
- Tell people what the piece is actually about. Summarize the substance, don't just tease it.
- Do NOT editorialize. No "this is fascinating," "everyone should read this," "mind-blowing," or similar. Let the content speak.
- Vary the structure. Don't always open with a personal anecdote. Could start with what the piece covers, a question it answers, a surprising detail from it, or a direct statement of the topic.
- 100–200 words total.
- 3–4 relevant hashtags leaning toward the content's actual topic. Conspiracy, paranormal, pop culture, and tech tags all welcome here.
- Final line, exactly as written: [Post written by AI Promotion Engine — article is all human]
- This post will be copy-pasted and posted manually. Write it ready to post as-is.
- Output the post text only, no surrounding explanation.
"""

BLUESKY_PROMPT_TEMPLATE = """\
Write a Bluesky post promoting this piece of content.

Content title: {title}
Source: {source}
URL: {url}
Description: {description}

Requirements:
- One or two plain sentences. Conversational, not performative.
- No em-dashes (—). Use a comma, period, or just cut the clause instead.
- Say what the piece is actually about and include the link.
- End with 2–3 relevant hashtags from the content's topic, plus #AIPromoted.
- The entire post including the URL must be under 280 characters total.
- Keep the body text short to leave room for the URL and hashtags.
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

    description = content.get("description", "")[:2500]

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

    facebook_prompt = FACEBOOK_PROMPT_TEMPLATE.format(
        title=content["title"],
        source=content["source"],
        url=content["url"],
        description=description,
    )
    facebook_post = call_claude(facebook_prompt)

    return {
        "linkedin": linkedin_post,
        "bluesky": bluesky_post,
        "facebook": facebook_post,
    }
