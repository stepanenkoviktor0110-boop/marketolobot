"""Adapted PMF stage prompts for LLM API (draft→polish).

Based on: github.com/alenazaharovaux/share/tree/main/skills/pmf
Each stage has:
  - questions: list of questions the bot asks the user
  - draft_prompt: generates structured JSON from user answers + context
  - polish_prompt: turns draft JSON into polished Markdown artifact
  - artifact: output filename
"""

STAGES = {
    "0_setup": {
        "name": "Настройка проекта",
        "questions": [
            "Как называется продукт? (полное имя)",
            "Какой тип продукта?\n• B2C SaaS\n• B2B SaaS\n• Marketplace\n• DTC\n• Services\n• Internal tool\n• Other",
            "Контекст организации?\n• Zero-to-one (с нуля)\n• Established (внутри работающей компании)\n• Extension (расширение существующего продукта)",
            "Founder-Market Fit: почему именно ты/ваша команда работает над этой идеей? Есть ли «earned secret» — знание про проблему/рынок из личного опыта?",
            "Каких ключевых компетенций не хватает? (продукт / инжиниринг / sales / маркетинг / domain)",
            "Насколько готов радикально изменить гипотезу если данные покажут что она неверна? (1-10, где 10 = полностью готов)",
        ],
        "draft_prompt": """Ты — PMF-аналитик. На основе ответов пользователя создай структурированный setup проекта.

Контекст проекта:
{context}

Ответы пользователя на вопросы setup-этапа:
{user_answers}

Верни СТРОГО JSON:
{{
  "product_name": "...",
  "product_type": "B2C SaaS | B2B SaaS | Marketplace | DTC | Services | Internal | Other",
  "org_context": "Zero-to-one | Established | Extension",
  "description": "краткое описание в 1-2 предложениях",
  "founder_market_fit": "...",
  "skill_gaps": ["gap1", "gap2"],
  "conviction_flexibility": 7,
  "risk_flag": "Высокий | Средний | Низкий",
  "risk_reasoning": "...",
  "notes": "..."
}}""",
        "polish_prompt": """Отредактируй черновик Setup-этапа PMF. Оформи как структурированный Markdown-документ.
Формат:
# Setup — {{product_name}}
**Slug:** ...
**Дата:** ...
**Стадия:** Stage 0 (Setup) → готов к Stage 1

## Продукт
## Team Pre-Flight Check
## Заметки
## Следующий шаг

Черновик:
{draft}""",
        "artifact": "00_setup.md",
    },

    "1_hypothesis": {
        "name": "Гипотеза (7 измерений PMF)",
        "questions": [
            "Какую проблему решает продукт? Опиши конкретный результат, к которому стремятся пользователи, и что им мешает его достичь.",
            "Кто целевая аудитория? Опиши через поведение (не демографию): что они делают сейчас, почему страдают, почему готовы платить.",
            "В чём ценностное предложение? Какой конкретный результат получает пользователь? (не фичи, а outcomes)",
            "В чём конкурентное преимущество? Выбери одну из 7 Powers Хелмера:\n• Scale Economies\n• Network Economies\n• Counter-Positioning\n• Switching Costs\n• Branding\n• Cornered Resource\n• Process Power",
            "Стратегия роста: как получишь первых 1000 пользователей? И как будешь масштабировать потом?",
            "Бизнес-модель: кто платит, сколько, как часто? Примерная юнит-экономика (LTV, CAC если есть оценки).",
            "Timing / Why Now: какое структурное изменение делает этот продукт возможным именно сейчас?",
        ],
        "draft_prompt": """Ты — PMF-аналитик. Создай гипотезу по 7 измерениям PMF.

Setup проекта:
{context}

Ответы пользователя:
{user_answers}

Верни СТРОГО JSON:
{{
  "dimensions": {{
    "problem": {{"statement": "...", "outcome": "...", "obstacle": "...", "confidence": 5}},
    "audience": {{"now_segment": "...", "future_segments": ["..."], "defining_attributes": ["..."], "confidence": 5}},
    "value_prop": {{"tagline": "...", "sub_benefits": ["..."], "confidence": 5}},
    "competitive_advantage": {{"power": "...", "explanation": "...", "landscape": {{"direct": ["..."], "indirect": ["..."]}}, "confidence": 5}},
    "growth": {{"short_term": "...", "long_term": "...", "confidence": 5}},
    "business_model": {{"revenue": "...", "pricing": "...", "ltv_estimate": "...", "cac_estimate": "...", "confidence": 5}},
    "timing": {{"trigger_event": "...", "why_not_earlier": "...", "window": "...", "confidence": 5}}
  }},
  "overall_confidence": 5,
  "riskiest_dimension": "...",
  "next_step": "..."
}}""",
        "polish_prompt": """Отредактируй черновик гипотезы PMF. Оформи как narrative-v1.md — структурированный Markdown.

Для каждого измерения:
- Чёткая формулировка
- Уровень уверенности (1-10) с обоснованием
- Что нужно проверить

В конце: общая оценка, самое рискованное измерение, следующий шаг.

Не приукрашивай. Если уверенность низкая — так и пиши.

Черновик:
{draft}""",
        "artifact": "narrative-v1.md",
    },

    "2_research": {
        "name": "Исследование рынка",
        "questions": [
            "Какие прямые конкуренты ты знаешь? Что у них хорошо/плохо?",
            "Какие косвенные решения используют люди сейчас (включая «ничего не делать» и «Excel-костыли»)?",
            "Есть ли данные о размере рынка? TAM/SAM/SOM оценки? Тренды?",
            "Знаешь ли аналоги (похожие продукты в других рынках, которые подтверждают гипотезу) или антилоги (которые опровергают)?",
        ],
        "draft_prompt": """Ты — аналитик рынка. Проведи структурированный анализ рынка для PMF-проекта.

Контекст (setup + гипотеза):
{context}

Ответы пользователя:
{user_answers}

Верни СТРОГО JSON:
{{
  "direct_competitors": [{{"name": "...", "strengths": ["..."], "weaknesses": ["..."], "positioning": "..."}}],
  "indirect_alternatives": ["..."],
  "market_size": {{"tam": "...", "sam": "...", "som": "...", "sources": ["..."]}},
  "trends": ["..."],
  "analogs": [{{"name": "...", "market": "...", "lesson": "..."}}],
  "antilogs": [{{"name": "...", "market": "...", "warning": "..."}}],
  "gaps_and_opportunities": ["..."],
  "impact_on_hypothesis": "..."
}}""",
        "polish_prompt": """Отредактируй черновик исследования рынка. Оформи как market-research.md.

Структура:
# Исследование рынка — {{product_name}}
## Конкурентный ландшафт
## Косвенные альтернативы
## Размер рынка
## Тренды
## Аналоги и антилоги
## Возможности
## Влияние на гипотезу

Усиль аргументацию. Добавь стратегический контекст. Не меняй факты.

Черновик:
{draft}""",
        "artifact": "market-research.md",
    },

    "3_synthesis": {
        "name": "Синтез и приоритизация рисков",
        "questions": [
            "После исследования рынка — что изменилось в твоём понимании? Какие измерения гипотезы усилились, какие ослабли?",
            "Какие риски кажутся самыми критичными? Что может убить продукт?",
            "Есть ли конфликты между измерениями? (например, модель монетизации не совместима с каналом роста)",
        ],
        "draft_prompt": """Ты — PMF-аналитик. Проведи синтез: пересмотри гипотезу в свете исследования рынка.

Контекст (setup + гипотеза + исследование):
{context}

Ответы пользователя:
{user_answers}

Верни СТРОГО JSON:
{{
  "confidence_changes": [{{"dimension": "...", "before": 5, "after": 6, "reason": "..."}}],
  "risks_prioritized": [
    {{"risk": "...", "dimension": "...", "severity": "high|medium|low", "likelihood": "high|medium|low", "mitigation": "..."}}
  ],
  "cross_fit_conflicts": ["..."],
  "narrative_updates": ["..."],
  "overall_confidence": 5,
  "recommendation": "proceed_to_validation | revisit_hypothesis | more_research",
  "next_step": "..."
}}""",
        "polish_prompt": """Отредактируй черновик синтеза. Создай ДВА документа в одном Markdown:

# Приоритизация рисков
(таблица рисков с severity/likelihood/mitigation)

---

# Narrative V2
(обновлённая версия гипотезы с учётом исследования)

Если уверенность упала — это ценно, не скрывай. Добавь рекомендацию по следующему шагу.

Черновик:
{draft}""",
        "artifact": "risk-prioritization.md",
        "extra_artifact": "narrative-v2.md",
    },

    "4_validation": {
        "name": "Валидация (DVF-фреймворк)",
        "questions": [
            "По каждому из 3 направлений DVF — какие предположения самые рискованные?\n• Desirability (хотят ли люди это?)\n• Viability (можно ли на этом заработать?)\n• Feasibility (можем ли мы это построить?)",
            "Для самого рискованного предположения — какой самый быстрый и дешёвый эксперимент можно провести?",
            "Какой порог успеха? При каком результате эксперимента ты скажешь «гипотеза подтверждена» vs «нужно менять»?",
        ],
        "draft_prompt": """Ты — PMF-аналитик. Создай карту предположений по DVF-фреймворку.

Контекст:
{context}

Ответы пользователя:
{user_answers}

Верни СТРОГО JSON:
{{
  "assumptions": {{
    "desirability": [{{"assumption": "...", "risk": "high|medium|low", "evidence_for": "...", "evidence_against": "..."}}],
    "viability": [{{"assumption": "...", "risk": "high|medium|low", "evidence_for": "...", "evidence_against": "..."}}],
    "feasibility": [{{"assumption": "...", "risk": "high|medium|low", "evidence_for": "...", "evidence_against": "..."}}]
  }},
  "priority_map": [{{"assumption": "...", "quadrant": "high_risk_high_impact | high_risk_low_impact | low_risk_high_impact | low_risk_low_impact"}}],
  "experiment": {{
    "target_assumption": "...",
    "method": "...",
    "success_threshold": "...",
    "timeline": "...",
    "resources_needed": "..."
  }},
  "next_step": "..."
}}""",
        "polish_prompt": """Отредактируй черновик валидации. Оформи как assumptions-map.md:

# Карта предположений (DVF)
## Desirability
## Viability
## Feasibility
## Приоритетная матрица (2×2)
## Эксперимент
## Следующий шаг

Черновик:
{draft}""",
        "artifact": "assumptions-map.md",
    },

    "5_interview_prep": {
        "name": "Подготовка к интервью",
        "questions": [
            "Кого будешь интервьюировать? Целевой сегмент, где их найти, сколько планируешь (рекомендация: 15-20).",
            "Какие главные вопросы хочешь задать? Что критично узнать от пользователей?",
            "Есть ли ограничения? (время, доступ к аудитории, язык)",
        ],
        "draft_prompt": """Ты — UX-исследователь. Создай гайд для пользовательских интервью.

Контекст (все предыдущие артефакты):
{context}

Ответы пользователя:
{user_answers}

Верни СТРОГО JSON:
{{
  "target_respondents": {{"segment": "...", "where_to_find": ["..."], "target_count": 15}},
  "screening_criteria": ["..."],
  "interview_sections": [
    {{"section": "Контекст", "questions": ["..."], "what_to_listen_for": "..."}},
    {{"section": "Проблема", "questions": ["..."], "what_to_listen_for": "..."}},
    {{"section": "Решение", "questions": ["..."], "what_to_listen_for": "..."}},
    {{"section": "Готовность платить", "questions": ["..."], "what_to_listen_for": "..."}}
  ],
  "dos_and_donts": {{"dos": ["..."], "donts": ["..."]}},
  "note_template": "..."
}}""",
        "polish_prompt": """Отредактируй черновик. Создай interview-guide.md — готовый к использованию гайд:

# Гайд для интервью — {{product_name}}
## Кого интервьюировать
## Скрининг
## Структура интервью (секции с вопросами)
## Do's and Don'ts
## Шаблон заметки

Гайд должен быть практичным — чтобы можно было открыть на телефоне и вести по нему интервью.

Черновик:
{draft}""",
        "artifact": "interview-guide.md",
    },

    "6_field": {
        "name": "Полевые интервью (вне бота)",
        "questions": [],
        "draft_prompt": "",
        "polish_prompt": "",
        "artifact": None,
    },

    "7_interview_synthesis": {
        "name": "Синтез интервью",
        "questions": [
            "Сколько интервью проведено? Краткое резюме: какие паттерны заметил?",
            "Что удивило? Что противоречит исходной гипотезе?",
            "Изменилась ли уверенность в каком-то из 7 измерений?",
        ],
        "draft_prompt": """Ты — PMF-аналитик. Синтезируй результаты пользовательских интервью.

Контекст (все артефакты + заметки интервью):
{context}

Ответы пользователя:
{user_answers}

Верни СТРОГО JSON:
{{
  "interviews_count": 0,
  "patterns": [{{"pattern": "...", "frequency": "...", "quotes": ["..."]}}],
  "surprises": ["..."],
  "hypothesis_contradictions": ["..."],
  "confidence_updates": [{{"dimension": "...", "before": 5, "after": 6, "reason": "..."}}],
  "overall_confidence": 5,
  "recommendation": "proceed_to_mvp | revisit_validation | pivot | more_interviews",
  "narrative_updates": ["..."]
}}""",
        "polish_prompt": """Отредактируй черновик. Создай ДВА документа:

# Синтез интервью
## Паттерны
## Сюрпризы
## Противоречия с гипотезой
## Обновление уверенности
## Рекомендация

---

# Narrative V3
(обновлённая версия с учётом интервью)

Черновик:
{draft}""",
        "artifact": "interview-synthesis.md",
        "extra_artifact": "narrative-v3.md",
    },

    "8_mvp_launch": {
        "name": "Запуск MVP (вне бота)",
        "questions": [],
        "draft_prompt": "",
        "polish_prompt": "",
        "artifact": None,
    },

    "9_metrics": {
        "name": "Метрики (Sean Ellis + retention)",
        "questions": [
            "Сколько активных пользователей у MVP?",
            "Результат Sean Ellis Survey: какой % ответил «very disappointed» если продукт исчезнет? (порог PMF: ≥40%)",
            "Данные по retention: DAU/WAU/MAU, когортный анализ если есть.",
            "Какие метрики бизнес-модели уже есть? (конверсия, revenue, unit economics)",
        ],
        "draft_prompt": """Ты — PMF-аналитик. Проанализируй метрики и определи уровень PMF.

Контекст:
{context}

Ответы пользователя:
{user_answers}

Верни СТРОГО JSON:
{{
  "active_users": 0,
  "sean_ellis_score": 0,
  "pmf_level": "Level 0 (no signal) | Level 1 (weak, <25%) | Level 2 (emerging, 25-39%) | Level 3 (strong, ≥40%)",
  "retention": {{"dau": 0, "wau": 0, "mau": 0, "d7_retention": "...", "d30_retention": "..."}},
  "business_metrics": {{"conversion": "...", "revenue": "...", "ltv": "...", "cac": "..."}},
  "diagnosis": "...",
  "recommendation": "scale | iterate_on_stage_4_or_7 | pivot_to_stage_1",
  "next_step": "..."
}}""",
        "polish_prompt": """Отредактируй черновик метрик. Оформи как metrics-dashboard.md:

# Метрики PMF — {{product_name}}
## Активные пользователи
## Sean Ellis Test (порог 40%)
## Retention
## Бизнес-метрики
## Диагноз уровня PMF
## Рекомендация

Будь честен. Если PMF не достигнута — прямо об этом скажи.

Черновик:
{draft}""",
        "artifact": "metrics-dashboard.md",
    },

    "10_iterate": {
        "name": "Итерация — решение",
        "questions": [
            "На основе метрик — что делаем?\n• Масштабируем (PMF достигнута)\n• Итерируем (возвращаемся к конкретному этапу)\n• Пивотим (новая гипотеза)",
            "Если итерируем — какие измерения нужно пересмотреть? Что именно изменим?",
            "Ключевые уроки из этого цикла?",
        ],
        "draft_prompt": """Ты — PMF-аналитик. Зафиксируй решение по итерации и ключевые выводы.

Контекст (все артефакты):
{context}

Ответы пользователя:
{user_answers}

Верни СТРОГО JSON:
{{
  "decision": "scale | iterate | pivot",
  "return_to_stage": "...",
  "changes_planned": ["..."],
  "lessons_learned": ["..."],
  "updated_dimensions": [{{"dimension": "...", "change": "..."}}],
  "timeline": "..."
}}""",
        "polish_prompt": """Отредактируй черновик. Оформи как iteration-changelog.md:

# Итерация — {{product_name}}
## Решение
## Куда возвращаемся
## Что меняем
## Уроки цикла
## Следующий шаг

Черновик:
{draft}""",
        "artifact": "iteration-changelog.md",
    },
}


def get_stage_questions(stage: str) -> list[str]:
    return STAGES.get(stage, {}).get("questions", [])


def get_draft_prompt(stage: str, context: str, user_answers: str) -> str:
    template = STAGES[stage]["draft_prompt"]
    return template.format(context=context, user_answers=user_answers)


def get_polish_prompt(stage: str, draft: str) -> str:
    template = STAGES[stage]["polish_prompt"]
    return template.format(draft=draft)


def get_artifact_name(stage: str) -> str | None:
    return STAGES[stage].get("artifact")


def get_extra_artifact_name(stage: str) -> str | None:
    return STAGES[stage].get("extra_artifact")


def is_manual_stage(stage: str) -> bool:
    """Stages where user does work outside the bot."""
    return stage in ("6_field", "8_mvp_launch")
