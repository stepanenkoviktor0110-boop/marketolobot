"""Text processing utilities for Telegram PMF bot."""


def chunk_text(text: str, max_len: int = 4000) -> list[str]:
    """Split text into Telegram-safe chunks (≤max_len chars each).

    Splits on paragraph boundaries first, then lines, then words.
    """
    if len(text) <= max_len:
        return [text]

    chunks = []
    # Split into paragraphs
    paragraphs = text.split("\n\n")
    current = ""

    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para

        if len(candidate) <= max_len:
            current = candidate
            continue

        # Current chunk is full — flush it
        if current:
            chunks.append(current.strip())
            current = ""

        # This paragraph alone fits
        if len(para) <= max_len:
            current = para
            continue

        # Paragraph too long — split by lines
        for line in para.split("\n"):
            candidate = f"{current}\n{line}" if current else line

            if len(candidate) <= max_len:
                current = candidate
                continue

            if current:
                chunks.append(current.strip())
                current = ""

            # Single line too long — split by words
            if len(line) <= max_len:
                current = line
                continue

            words = line.split(" ")
            for word in words:
                candidate = f"{current} {word}" if current else word
                if len(candidate) <= max_len:
                    current = candidate
                else:
                    if current:
                        chunks.append(current.strip())
                    current = word

    if current.strip():
        chunks.append(current.strip())

    return chunks


def format_status(
    project_name: str,
    stage: str,
    stage_name: str,
    confidence: int | None = None,
) -> str:
    """Format a short status message for Telegram."""
    msg = f"📊 Проект: {project_name}\n🔹 Этап: {stage_name} ({stage})"
    if confidence is not None:
        msg += f"\n📈 Уверенность: {confidence}/10"
    return msg


def format_stage_intro(
    stage: str,
    stage_name: str,
    questions: list[str],
) -> str:
    """Format intro message when entering a new stage."""
    lines = [f"📋 Этап: {stage_name}\n"]
    if questions:
        lines.append(f"Тебе предстоит ответить на {len(questions)} вопрос(ов).\n")
        lines.append("Вопросы этапа:")
        for i, q in enumerate(questions, 1):
            # Show shortened version in intro
            short = q.split("\n")[0]
            lines.append(f"  {i}. {short}")
        lines.append("\nОтвечай на каждый вопрос по очереди. Начинаем 👇")
    else:
        lines.append("Этот этап выполняется вне бота.")
    return "\n".join(lines)
