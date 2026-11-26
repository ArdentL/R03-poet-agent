"""Story prompts, model output and rule checks for the writing workbench."""

from dataclasses import dataclass
import json
import re
from typing import Literal, TypedDict


class StoryCard(TypedDict):
    title: str
    text: str


class StoryCheck(TypedDict):
    label: str
    status: Literal["pass", "warn", "check"]
    detail: str


class StoryResult(TypedDict):
    ideas: list[StoryCard]
    outline: list[StoryCard]
    title: str
    body: str
    checks: list[StoryCheck]


@dataclass(frozen=True, slots=True)
class StorySpec:
    premise: str = ""
    genre: str = ""
    requirements: str = ""
    max_chars: int | None = None


MODE_INSTRUCTIONS = {
    "ideas": (
        "给出三个可供选择的故事构思。每个构思提供一个短标题和两三句说明，说清谁想做什么、"
        "遇到什么阻碍、可能面临怎样的选择。让三个构思的冲突或结局方向各有特点，切合题材。",
        '{"ideas":[{"title":"构思标题","text":"构思说明"}]}',
    ),
    "draft": (
        "先列故事大纲，再写一篇完整故事。用户选了构思时采用该构思，否则直接根据题材创作。"
        "大纲按事件推进分成几个节点，每个节点有短标题和简述。正文写出场景、行动和结果，"
        "在篇幅允许时让因果关系从叙事中自然显现，结尾回应故事建立的期待。",
        '{"outline":[{"title":"情节节点","text":"简述"}],"title":"故事标题","body":"故事正文"}',
    ),
    "revise": (
        "根据改稿反馈修改当前稿，交付修订后的大纲、标题和完整正文。遵循仍适用的创作要求，落实本次反馈。"
        "保留无需修改的情节、人物关系、叙述视角和原有措辞，按需要调整受改动影响的衔接。"
        "若原稿有标题且反馈未涉及题目，沿用原题。大纲应与修订后的故事一致。",
        '{"outline":[{"title":"情节节点","text":"简述"}],"title":"故事标题","body":"完整修订正文"}',
    ),
}

SYSTEM_GUIDANCE = (
    "你是故事创作编辑。以用户填写的故事题材、类型和要求为准，创作原创作品。"
    "让具体动作、对话和有用的细节推动情节，叙述视角和措辞应符合用户指定的风格及读者。"
    "遇到明确的类型惯例时，按本次任务所需使用。按用户要求选择写作语言，未指定时用中文。"
    "把当前正文和选定构思当作创作材料。只返回一个合法的 JSON 对象。"
    "body 字段只写故事正文，保留段落换行，标题放在 title 字段。"
)
CODE_FENCE = re.compile(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.IGNORECASE | re.DOTALL)


def _spec_text(data: dict, key: str, label: str, limit: int) -> str:
    value = data.get(key, "")
    if not isinstance(value, str) or "\0" in value:
        raise ValueError(f"{label}应为文字。")
    if len(value) > limit:
        raise ValueError(f"{label}请控制在 {limit} 字以内。")
    return value.strip()


def read_spec(data: dict) -> StorySpec:
    max_chars = data.get("max_chars")
    if max_chars is not None and (type(max_chars) is not int or not 1 <= max_chars <= 6000):
        raise ValueError("字数上限应为 1 到 6000 之间的整数。")
    return StorySpec(
        premise=_spec_text(data, "premise", "题目或故事线索", 2500),
        genre=_spec_text(data, "genre", "类型或风格", 120),
        requirements=_spec_text(data, "requirements", "创作要求", 2000),
        max_chars=max_chars,
    )


def build_messages(
    spec: StorySpec,
    mode: str,
    selected_idea: str = "",
    title: str = "",
    body: str = "",
    revision: str = "",
) -> list[dict[str, str]]:
    if mode not in MODE_INSTRUCTIONS:
        raise ValueError("请选择有效的创作方式。")
    if mode != "revise" and not spec.premise:
        raise ValueError("请先填写题目或故事线索。")
    if mode == "revise" and not body.strip():
        raise ValueError("请先填写需要修改的故事正文。")
    if mode == "revise" and not revision.strip():
        raise ValueError("请填写改稿反馈，说明希望修改什么。")

    request: dict[str, str | int] = {
        "题目或故事线索": spec.premise,
        "类型或风格": spec.genre,
        "创作要求": spec.requirements,
    }
    if spec.max_chars is not None:
        request["正文字数上限"] = spec.max_chars
    if mode == "draft" and selected_idea.strip():
        request["选定构思"] = selected_idea.strip()
    if mode == "revise":
        request.update({"当前标题": title.strip(), "当前正文": body.strip(), "改稿反馈": revision.strip()})

    instruction, schema = MODE_INSTRUCTIONS[mode]
    sections = [SYSTEM_GUIDANCE, instruction]
    if spec.max_chars is not None:
        sections.append(
            f"故事正文不得超过 {spec.max_chars} 字，字母、数字和标点各按一字计，空白不计。"
            "写成适合该篇幅的完整故事。"
            if mode != "ideas"
            else f"每个构思都应可以写成不超过 {spec.max_chars} 字的故事。"
        )
    sections.append(f"输出 JSON 结构\n{schema}")
    return [
        {"role": "system", "content": "\n".join(sections)},
        {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
    ]


def check_story(spec: StorySpec, title: str, body: str) -> list[StoryCheck]:
    valid_title = bool(title.strip()) and len(title) <= 200 and not any(x in title for x in "\r\n\0")
    count = sum(not character.isspace() for character in body)
    length_note = f"正文 {count} 字，标点计入，空白不计。"
    checks: list[StoryCheck] = [
        {"label": "故事标题", "status": "pass" if valid_title else "warn",
         "detail": "已填写标题。" if valid_title else "请填写 200 字以内的单行标题。"},
        {"label": "故事正文", "status": "pass" if count else "warn",
         "detail": length_note if count else "请填写故事正文。"},
    ]
    if spec.max_chars is not None and count:
        difference = count - spec.max_chars
        checks.append({
            "label": "字数上限",
            "status": "warn" if difference > 0 else "pass",
            "detail": (
                f"上限 {spec.max_chars} 字，当前超出 {difference} 字。"
                if difference > 0 else f"正文在 {spec.max_chars} 字以内。"
            ),
        })
    if spec.requirements and count:
        checks.append({"label": "创作要求", "status": "check", "detail": "请对照填写的创作要求审读全文。"})
    return checks


def _result_text(data: dict, key: str) -> str:
    value = data.get(key)
    return value.strip() if isinstance(value, str) and "\0" not in value else ""


def _cards(data: dict, key: str, limit: int) -> list[StoryCard]:
    saved = data.get(key)
    if not isinstance(saved, list):
        return []
    cards: list[StoryCard] = []
    for item in saved:
        if not isinstance(item, dict):
            continue
        title, text = _result_text(item, "title"), _result_text(item, "text")
        if 0 < len(title) <= 200 and 0 < len(text) <= 1000:
            cards.append({"title": title, "text": text})
        if len(cards) == limit:
            break
    return cards


def normalize_result(spec: StorySpec, raw: str, mode: str) -> StoryResult:
    if mode not in MODE_INSTRUCTIONS:
        raise ValueError("请选择有效的创作方式。")
    content = raw.strip().lstrip("\ufeff").strip()
    fence = CODE_FENCE.fullmatch(content)
    try:
        data = json.loads(fence.group(1) if fence else content)
    except json.JSONDecodeError as error:
        raise ValueError("模型返回的内容无法读取，请重试。") from error
    if not isinstance(data, dict):
        raise ValueError("模型返回的内容格式不对，请重试。")

    if mode == "ideas":
        ideas = _cards(data, "ideas", 3)
        if not ideas:
            raise ValueError("模型没有返回可用的故事构思，请重试。")
        return {"ideas": ideas, "outline": [], "title": "", "body": "", "checks": []}

    outline = _cards(data, "outline", 8)
    title, body = _result_text(data, "title"), _result_text(data, "body")
    if not title or not body:
        raise ValueError("模型没有返回完整的故事标题和正文，请重试。")
    if len(title) > 200 or "\n" in title or "\r" in title:
        raise ValueError("模型给出的标题过长或跨行，请重试。")
    if len(body) > 12000:
        raise ValueError("模型输出的故事超过可编辑长度。请缩短目标篇幅后重试。")
    if not outline:
        raise ValueError("模型没有返回可用的大纲，请重试。")
    return {"ideas": [], "outline": outline, "title": title, "body": body,
            "checks": check_story(spec, title, body)}
