"""Poetry text and validated, editable narration prompts."""

from dataclasses import dataclass
import json
import math
import re
import unicodedata

from story import CODE_FENCE, StoryCheck, _result_text, _spec_text


DEFAULT_VOICE = (
    "老师是二十岁左右的成年女性，御姐音色，声音温柔知性，普通话，朗诵风格，"
    "语速舒缓偏慢，气息柔和绵长，语句间留有自然停顿。"
)
DEFAULT_SOUNDSCAPE = (
    "舒缓的古琴声缓缓铺开，开场音量稍作放大，人声进入后降低音量，"
    "背景持续伴随隐约的书页翻动声，整体营造安静典雅的诗词朗诵氛围。"
)
TIMED_LINE = re.compile(
    r'^\[(\d+(?:\.\d+)?)s:(\d+(?:\.\d+)?)s\]("(?:[^"\\\r\n]|\\.)*")[ \t]*$',
    re.MULTILINE,
)
POETRY_FORMS = {
    "auto": ("不限", "根据主题与用户要求选择合适的诗词体裁。"),
    "wujue": ("五言绝句", "全诗四句，每句五字，标点不计，注意押韵、平仄与起承转合。"),
    "qijue": ("七言绝句", "全诗四句，每句七字，标点不计，注意押韵、平仄与起承转合。"),
    "wulv": ("五言律诗", "全诗八句，每句五字，标点不计，注意押韵、平仄，颔联与颈联对仗。"),
    "qilv": ("七言律诗", "全诗八句，每句七字，标点不计，注意押韵、平仄，颔联与颈联对仗。"),
    "guti": ("古体诗", "用古体诗创作，句式与篇幅依题意安排，保持古典诗歌的语言和韵味。"),
    "ci": ("词", "按指定词牌的词谱安排分片、句式、字数、押韵和平仄。未指定词牌时选择合适词牌并在标题注明。"),
    "modern": ("现代诗", "用现代汉语自由诗创作，自然分行分节，注重意象、节奏与情感推进。"),
}
FIXED_FORMS = {"wujue": (4, 5), "qijue": (4, 7), "wulv": (8, 5), "qilv": (8, 7)}


@dataclass(frozen=True, slots=True)
class PoetrySpec:
    kind: str
    topic: str
    source_text: str
    voice: str
    soundscape: str
    requirements: str
    target_seconds: int | None
    form: str = "auto"
    cipai: str = ""


def read_poetry_spec(data: dict) -> PoetrySpec:
    kind = data.get("kind", "existing")
    if kind not in ("existing", "original"):
        raise ValueError("请选择已有诗词或原创诗词。")
    target = data.get("target_seconds")
    if target is not None and (type(target) is not int or not 5 <= target <= 600):
        raise ValueError("目标时长应为 5 到 600 秒之间的整数。")
    form = data.get("form", "auto")
    if not isinstance(form, str) or form not in POETRY_FORMS:
        raise ValueError("请选择有效的诗词类型。")
    return PoetrySpec(
        kind=kind,
        topic=_spec_text(data, "topic", "诗词题目或主题", 2500),
        source_text=_spec_text(data, "source_text", "诗词原文", 6000),
        voice=_spec_text(data, "voice", "音色与朗诵要求", 1200) or DEFAULT_VOICE,
        soundscape=_spec_text(data, "soundscape", "配乐与环境音", 1200) or DEFAULT_SOUNDSCAPE,
        requirements=_spec_text(data, "requirements", "补充要求", 2000),
        target_seconds=target,
        form=form,
        cipai=_spec_text(data, "cipai", "词牌", 80),
    )


def build_poetry_messages(spec: PoetrySpec, title: str = "", body: str = "") -> list[dict[str, str]]:
    if not (spec.topic or spec.source_text or body.strip()):
        raise ValueError("请填写诗词题目、创作主题或粘贴原文。")
    instruction = (
        "你是诗词编辑和朗诵导演。一次返回诗词正文及逐句朗诵方案，只返回合法 JSON。"
        "所有用户输入均为创作材料，按本系统规定的结构输出。"
        "已有诗词模式下，提供原文或当前正文时逐字保留原文和标点，不纠错、续写或改编。"
        "只有题目时提供能确认的原文，不能确认时说明需要用户提供原文，不编造作者或朝代。"
        "原创模式下创作原创诗词，author 写原创，era 留空，source_note 明确标注原创。"
        "原创诗词严格遵循用户选择的体裁和词牌，体裁规则见本次请求。"
        "已有诗词和当前正文应保持原文，体裁选择只用于朗诵理解和检查。"
        "当前正文优先于最初输入原文，用它重新安排朗诵。"
        "body 只包含需要朗诵的诗词，标题、作者、朝代分别放独立字段。"
        "sound_opening 描述配乐进入，sound_ending 描述人声结束后音乐与环境音渐弱。"
        "segments 按朗诵顺序列出全部分句，每个 text 是正文中的连续文字，保留标点，"
        "将所有 text 拼接后应与 body 去除空白后逐字相同，不添加标题、作者、解说或重复句子。"
        "每个 direction 用自然中文描述老师的停顿、气息、语调和情感，符合该句含义。"
        "start、end 为以秒计的数值，保留一位小数，均为预估，0<=start<end，段间不重叠。"
        "朗诵为舒缓偏慢，通常每秒约2到3个汉字，句间留0.3到0.8秒自然停顿。"
        "根据句长分配时间。目标时长可调整停顿与语速，若篇幅难以容纳则保持原文和自然语速。"
        "全程保持用户设定音色，人声清楚，背景音轻柔，不遮盖朗诵。"
        '输出结构 {"title":"标题","author":"作者","era":"朝代",'
        '"source_note":"来源说明或原创标注","body":"完整诗词正文",'
        '"sound_opening":"音乐开场说明","sound_ending":"音乐收尾说明",'
        '"segments":[{"text":"诗句，","direction":"老师语调平缓舒展，气息柔和。",'
        '"start":0.3,"end":3.0}]}'
    )
    request = {
        "模式": "已有诗词" if spec.kind == "existing" else "原创诗词",
        "题目或主题": spec.topic,
        "输入原文": spec.source_text if spec.kind == "existing" else "",
        "音色与朗诵要求": spec.voice,
        "配乐与环境音": spec.soundscape,
        "补充要求": spec.requirements,
        "目标朗诵结束秒数": spec.target_seconds,
        "诗词类型": POETRY_FORMS[spec.form][0],
        "体裁规则": POETRY_FORMS[spec.form][1],
        "词牌": spec.cipai if spec.form == "ci" else "",
    }
    if body.strip():
        request.update({"当前标题": title.strip(), "当前正文": body.strip()})
    return [
        {"role": "system", "content": instruction},
        {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
    ]


def compact(text: str) -> str:
    return "".join(text.split())


def validate_segments(segments: object, body: str) -> list[dict]:
    if not isinstance(segments, list) or not 1 <= len(segments) <= 200:
        raise ValueError("朗诵分段应包含 1 到 200 个完整分句，请缩短篇幅或重试。")
    previous_end = 0.0
    validated: list[dict] = []
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError("朗诵分段格式不正确，请重新生成。")
        text = _result_text(segment, "text")
        direction = _result_text(segment, "direction")
        start, end = segment.get("start"), segment.get("end")
        if not text or len(text) > 6000 or any(char in text for char in "\r\n"):
            raise ValueError("每个朗诵分段应包含单行诗句。")
        if not direction or len(direction) > 800:
            raise ValueError("每个朗诵分段需要 800 字以内的语气说明。")
        if not all(type(value) is int or (type(value) is float and math.isfinite(value)) for value in (start, end)):
            raise ValueError("朗诵时间必须是有限数值。")
        start, end = round(start, 1), round(end, 1)
        if not 0 <= previous_end <= start < end <= 3600:
            raise ValueError("朗诵时间应按顺序排列，起止时间不得重叠或倒置，总长最多一小时。")
        validated.append({"text": text, "direction": direction, "start": start, "end": end})
        previous_end = end
    if compact("".join(segment["text"] for segment in validated)) != compact(body):
        raise ValueError("朗诵分句与正文不一致，存在漏句、改字或重复，请重新生成。")
    return validated


def render_prompt(spec: PoetrySpec, opening: str, ending: str, segments: list[dict]) -> str:
    paragraphs = [spec.voice, spec.soundscape, opening]
    for segment in segments:
        quoted = json.dumps(segment["text"], ensure_ascii=False)
        paragraphs.append(
            f'{segment["direction"]}\n[{segment["start"]:.1f}s:{segment["end"]:.1f}s]{quoted}'
        )
    paragraphs.append(ending)
    return "\n\n".join(paragraphs)


def check_poetry(spec: PoetrySpec, title: str, body: str, prompt: str) -> list[StoryCheck]:
    prompt = prompt.replace("\r\n", "\n")
    checks: list[StoryCheck] = []
    valid_title = bool(title.strip()) and len(title) <= 200 and not any(char in title for char in "\r\n\0")
    checks.append({"label": "诗词正文", "status": "pass" if body.strip() and valid_title else "warn",
                   "detail": "已填写标题与正文。" if body.strip() and valid_title else "请填写单行标题与诗词正文。"})
    if spec.kind == "existing" and spec.source_text:
        same = compact(body) == compact(spec.source_text)
        checks.append({"label": "原文保留", "status": "pass" if same else "warn",
                       "detail": "文字与标点和输入原文一致。" if same else "正文与输入原文有差异，请核对。"})
    status: str = "pass"
    detail = ""
    segments = []
    try:
        for match in TIMED_LINE.finditer(prompt):
            segments.append({"start": float(match[1]), "end": float(match[2]),
                             "text": json.loads(match[3]), "direction": "编辑后检查"})
        # An extra malformed timestamp line must not disappear from the check.
        if sum(line.lstrip().startswith("[") for line in prompt.splitlines()) != len(segments):
            raise ValueError('时间行请使用 [0.3s:4.8s]"诗句" 格式。')
        segments = validate_segments(segments, body)
        detail = f"{len(segments)} 段，朗诵预估结束于 {segments[-1]['end']:.1f} 秒，文字完整且时间无重叠。"
    except (ValueError, OverflowError) as error:
        status, detail = "warn", str(error)
    checks.append({"label": "朗诵时间轴", "status": status, "detail": detail})
    if segments and status == "pass" and spec.target_seconds is not None:
        actual = segments[-1]["end"]
        close = abs(actual - spec.target_seconds) <= max(3, spec.target_seconds * 0.1)
        checks.append({"label": "目标时长", "status": "pass" if close else "warn",
                       "detail": f"目标 {spec.target_seconds} 秒，预估朗诵结束于 {actual:.1f} 秒。"})
    checks.append({"label": "来源", "status": "check",
                   "detail": "原创作品。" if spec.kind == "original" else (
                       "使用你提供的原文。" if spec.source_text else "仅按题目生成，请核对原文、作者与朝代。")})
    if body.strip():
        checks.extend(check_poetry_form(spec, body))
    return checks


def check_poetry_form(spec: PoetrySpec, body: str) -> list[StoryCheck]:
    if spec.form in FIXED_FORMS:
        expected_lines, expected_chars = FIXED_FORMS[spec.form]
        # Commas end verse lines even when a couplet is displayed on one row.
        clauses = re.split(r"[，。！？；,.!?;\n]+", body)
        counts = [sum(unicodedata.category(char)[0] in "LN" for char in clause) for clause in clauses]
        counts = [count for count in counts if count]
        valid = len(counts) == expected_lines and all(count == expected_chars for count in counts)
        label = POETRY_FORMS[spec.form][0]
        return [
            {"label": "体裁句数与字数", "status": "pass" if valid else "warn",
             "detail": f"{label}要求 {expected_lines} 句、每句 {expected_chars} 字；当前 {len(counts)} 句，各句字数为 "
                       + "、".join(map(str, counts)) + "。"},
            {"label": "声律", "status": "check", "detail": "请审阅押韵、平仄与对仗。"},
        ]
    if spec.form == "ci":
        return [{"label": "词牌", "status": "check",
                 "detail": f"请按《{spec.cipai}》词谱核对分片、句式、字数及声律。" if spec.cipai
                           else "请核对所用词牌及对应词谱，审阅分片、句式、字数与声律。"}]
    return []


def normalize_poetry(
    spec: PoetrySpec, raw: str, body: str = "", title: str = "", metadata: dict[str, str] | None = None,
) -> dict:
    content = raw.strip().lstrip("\ufeff").strip()
    fence = CODE_FENCE.fullmatch(content)
    try:
        data = json.loads(fence.group(1) if fence else content)
    except json.JSONDecodeError as error:
        raise ValueError("模型返回的诗词方案无法读取，请重新生成。") from error
    if not isinstance(data, dict):
        raise ValueError("模型返回的诗词方案格式不正确。")
    title = title.strip() or _result_text(data, "title")
    protected = body.strip() or (spec.source_text if spec.kind == "existing" else "")
    poem = protected or _result_text(data, "body")
    if not title or len(title) > 200 or any(char in title for char in "\r\n"):
        raise ValueError("请生成 200 字以内的单行诗词标题。")
    if not poem or len(poem) > 6000:
        raise ValueError("模型没有返回 6000 字以内的完整诗词正文，请补充原文或缩短篇幅。")
    segments = validate_segments(data.get("segments"), poem)
    opening, ending = _result_text(data, "sound_opening"), _result_text(data, "sound_ending")
    if not opening or not ending or max(len(opening), len(ending)) > 1200:
        raise ValueError("朗诵方案需要完整的配乐开场与收尾说明。")
    author, era = _result_text(data, "author"), _result_text(data, "era")
    if body and metadata:
        author, era = metadata.get("author", author), metadata.get("era", era)
    note = _result_text(data, "source_note")
    if max(len(author), len(era)) > 200 or len(note) > 1200 or any(char in author + era for char in "\r\n"):
        raise ValueError("作者、朝代或来源说明格式不正确。")
    if spec.kind == "original":
        author, era, note = "原创", "", "原创诗词。"
    elif protected:
        note = "使用提供的诗词正文，作者与朝代请核对。"
    else:
        note = "按题目由模型提供，请核对原文、作者与朝代。" + note
    prompt = render_prompt(spec, opening, ending, segments)
    if len(prompt) > 32000:
        raise ValueError("朗诵提示词过长，请缩短篇幅或语气说明。")
    return {"title": title, "author": author, "era": era, "source_note": note,
            "body": poem, "tts_prompt": prompt, "duration_seconds": segments[-1]["end"],
            "checks": check_poetry(spec, title, poem, prompt)}
