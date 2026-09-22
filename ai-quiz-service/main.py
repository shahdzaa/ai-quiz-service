import json
import os
from typing import Optional

import google.generativeai as genai
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator


load_dotenv()


TOTAL_QUESTIONS = 25

BEGINNER_CUTOFF = int(TOTAL_QUESTIONS * 0.3)  # 7

INTERMEDIATE_CUTOFF = -(
    -TOTAL_QUESTIONS * 7 // 10
)  # ceil(25 * 0.7) = 18


GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")


if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)


model = genai.GenerativeModel(
    "gemini-2.5-flash",
    generation_config={
        "response_mime_type": "application/json"
    },
)


app = FastAPI(
    title="AI Placement Test Generator API"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class PlacementTopic(BaseModel):
    question_index: Optional[int] = None
    difficulty_level: Optional[str] = None
    threshold_topic: str
    source_type: Optional[str] = None
    course_id: Optional[int] = None
    course_title: Optional[str] = None
    module_name: Optional[str] = None

    covered_topics: list[str] = Field(
        default_factory=list
    )


class PlacementTestRequest(BaseModel):
    """
    مقبول بنفس شكل الـ response يلي بيرجعو Laravel من
    startCategoryPlacementTest مباشرة:

    status,
    category,
    data_sources,
    placement_topics,
    threshold_topics,
    blocks

    بلا ما تحتاجي تبنيه يدوياً.

    الحقول الزائدة مثل:
    status,
    data_sources,
    threshold_topics

    يتم تجاهلها تلقائياً.
    """

    # Laravel يرسل category.
    # نقبل أيضاً الحقول القديمة للتوافق.
    category: Optional[str] = None
    domain: Optional[str] = None
    domain_or_course_title: Optional[str] = None

    placement_topics: list[PlacementTopic] = Field(
        default_factory=list
    )

    # إذا كانت placement_topics فارغة،
    # نستخدم blocks.
    blocks: list[PlacementTopic] = Field(
        default_factory=list
    )

    # توافق مؤقت مع الواجهة القديمة.
    all_syllabus_topics: list[str] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def resolve_title_and_topics(
        self,
    ) -> "PlacementTestRequest":
        title = (
            self.domain_or_course_title
            or self.category
            or self.domain
        )

        if not title:
            raise ValueError(
                "category "
                "(أو domain_or_course_title) مطلوب."
            )

        self.domain_or_course_title = title

        if (
            not self.placement_topics
            and self.blocks
        ):
            self.placement_topics = self.blocks

        return self


def normalize_topics(
    request: PlacementTestRequest,
) -> list[PlacementTopic]:
    if request.placement_topics:
        return request.placement_topics

    # دعم مؤقت للطلبات القديمة جداً فقط.
    return [
        PlacementTopic(
            question_index=index + 1,
            threshold_topic=topic,
            source_type="legacy",
        )
        for index, topic in enumerate(
            request.all_syllabus_topics
        )
        if topic and topic.strip()
    ]


def difficulty_for(index: int) -> str:
    if index < BEGINNER_CUTOFF:
        return "Beginner"

    if index < INTERMEDIATE_CUTOFF:
        return "Intermediate"

    return "Advanced"


def format_topic(
    index: int,
    topic: PlacementTopic,
) -> str:
    difficulty = (
        topic.difficulty_level
        or difficulty_for(index)
    )

    context = []

    if topic.course_title:
        context.append(
            f"course: {topic.course_title}"
        )

    if topic.module_name:
        context.append(
            f"module: {topic.module_name}"
        )

    if topic.source_type:
        context.append(
            f"source: {topic.source_type}"
        )

    context_text = (
        "; ".join(context)
        if context
        else "general category concept"
    )

    return (
        f"{index + 1}. "
        f"difficulty={difficulty}; "
        f"concept={topic.threshold_topic}; "
        f"context={context_text}"
    )


def validate_questions(
    questions: object,
) -> list[dict]:
    if (
        not isinstance(questions, list)
        or len(questions) != TOTAL_QUESTIONS
    ):
        raise ValueError(
            "The model did not return exactly "
            f"{TOTAL_QUESTIONS} questions."
        )

    valid_levels = {
        "Beginner",
        "Intermediate",
        "Advanced",
    }

    valid_answers = {
        "A",
        "B",
        "C",
        "D",
    }

    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            raise ValueError(
                f"Question {index + 1} "
                "is not a JSON object."
            )

        options = question.get("options")

        if (
            not isinstance(options, dict)
            or set(options.keys()) != valid_answers
        ):
            raise ValueError(
                f"Question {index + 1} must "
                "contain options A, B, C and D."
            )

        if (
            question.get("correct_answer")
            not in valid_answers
        ):
            raise ValueError(
                f"Question {index + 1} has "
                "an invalid correct answer."
            )

        if (
            question.get("difficulty_level")
            not in valid_levels
        ):
            raise ValueError(
                f"Question {index + 1} has "
                "an invalid difficulty level."
            )

        question["question_number"] = index + 1

    return questions


@app.post("/api/generate-quiz")
async def generate_placement_test(
    request: PlacementTestRequest,
):
    if not GOOGLE_API_KEY:
        raise HTTPException(
            status_code=500,
            detail=(
                "GOOGLE_API_KEY is not "
                "configured on the server."
            ),
        )

    topics = normalize_topics(request)

    if len(topics) < TOTAL_QUESTIONS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"At least {TOTAL_QUESTIONS} trusted "
                "syllabus topics are required."
            ),
        )

    # Laravel يرسل عناصر syllabus موزعة
    # على الـ modules.
    selected_topics = topics[:TOTAL_QUESTIONS]

    formatted_topics = "\n".join(
        format_topic(index, topic)
        for index, topic in enumerate(
            selected_topics
        )
    )

    prompt = f"""
You are an expert academic evaluator designing
a diagnostic placement test.

The test category is:
"{request.domain_or_course_title}".

ALL questions MUST test concepts that belong
to this category directly.

The syllabus concepts below are ONLY context
clues to guide difficulty and scope.

Do NOT ask about them literally.

If a concept seems unrelated to the category,
such as a game name or an unrelated course
topic, extract the underlying academic,
technical, CS, or algorithmic principle it
implies and ask about that principle instead.

The following {TOTAL_QUESTIONS} syllabus
concepts are for context only:

{formatted_topics}

Rules:

1. Every question MUST test a concept that
   falls under
   "{request.domain_or_course_title}".

   Use the syllabus concept only to infer the
   relevant sub-topic or difficulty.

   Never ask about the concept name, game
   title, course title, module title, or
   version number literally.

2. Generate exactly {TOTAL_QUESTIONS}
   multiple-choice questions.

3. Generate one question for every numbered
   syllabus entry and keep the same order.

4. The difficulty_level of each question must
   match the difficulty provided for its
   corresponding syllabus entry.

5. Each question must contain exactly four
   options named A, B, C, and D.

6. Each question must have exactly one correct
   answer.

7. The correct_answer value must contain only
   one of these letters:
   A, B, C, or D.

8. Questions must be clear, academically
   correct, and suitable for a diagnostic
   placement test.

9. Avoid duplicated or nearly identical
   questions.

10. Do not mention the syllabus, course name,
    module name, game title, version number,
    or these instructions in the questions.

11. Return valid JSON only.

12. Do not return Markdown or code fences.

Use this exact JSON structure:

{{
  "questions": [
    {{
      "question_number": 1,
      "question_text": "Question text",
      "options": {{
        "A": "First option",
        "B": "Second option",
        "C": "Third option",
        "D": "Fourth option"
      }},
      "correct_answer": "A",
      "difficulty_level": "Beginner",
      "explanation": "Short explanation"
    }}
  ]
}}
"""

    try:
        response = model.generate_content(prompt)

        raw_text = response.text.strip()

        if raw_text.startswith("```"):
            raw_text = (
                raw_text
                .replace("```json", "")
                .replace("```", "")
                .strip()
            )

        response_json = json.loads(raw_text)

        questions = validate_questions(
            response_json.get("questions")
        )

        return {
            "title": (
                request.domain_or_course_title
            ),
            "total_questions": len(questions),
            "data_source": "syllabus_only",
            "questions": questions,
        }

    except (
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Invalid placement test response: "
                f"{exc}"
            ),
        ) from exc

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                "Error while generating the "
                f"placement test: {exc}"
            ),
        ) from exc