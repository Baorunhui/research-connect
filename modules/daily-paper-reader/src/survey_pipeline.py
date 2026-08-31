"""原生综述流水线：主题 → 召回 → 精排 → 抽取 → 聚类 → 深读 → 分析 → 成文 → 审校。

编排结构平移自原 paper_agent 多智能体项目已验证的设计（该外部子项目已移除，本模块为原生实现）：
- 聚类：嵌入文本用抽取字段拼接（Problem/Method/Results/Contributions），肘部法则自动选 k；
- 分析：每簇深析（4 维度）+ 全局分析（6 模块），两级 LLM 分析共同构成写作上下文；
- 大纲：导演模式，每节标注覆盖簇（cluster_ids / all_clusters），叙事结构与聚类解耦；
- 写作：分节并行 + 并发闸门（paper_agent 实测教训：全部小节同时并行会打爆 LLM 端点 429）；
- 引用：只能引用给定编号 [n]，装配时校验并剔除非法引用。

依赖全部来自本仓库 src/ 基建：Supabase 召回（2.1/2.2）、RRF（2.3）、reranker（3.rank_papers）、
DeepSeek 客户端（llm.py）、PDF 全文（6.generate_docs.ensure_text_content）。
sklearn 为可选依赖（缺失时聚类退化为按序等分）。
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from conference_sidebar import slugify  # noqa: E402
from llm import (  # noqa: E402
    DeepSeekClient,
    resolve_llm_api_key,
    resolve_llm_base_url,
    resolve_llm_model,
)
from supabase_source import get_supabase_read_config  # noqa: E402

EMBED_MODEL_NAME = "BAAI/bge-small-en-v1.5"
RRF_K = 60
RECALL_TOP_K = 200
DEFAULT_MAX_PAPERS = 30
DEFAULT_FETCH_DAYS = 9
DEFAULT_EXTRACT_CONCURRENCY = 4
# 写作/簇深析并发闸门：调大易触发 LLM 端点限流（429），paper_agent 已实测踩坑。
DEFAULT_WRITE_CONCURRENCY = 2
DEEP_READ_PER_CLUSTER = 2
DEEP_READ_TEXT_CHAR_CAP = 12000
CLUSTER_MAX_K = 5
RELEVANCE_MIN_SCORE = 4.0
# Kaggle 快照粗筛量级（万级候选）与语义粗排收窄目标（进 rerank 的池大小）
DEFAULT_KAGGLE_COARSE_TOP_K = 10000
DEFAULT_EMBED_POOL = 300
# 任务范式一致性门槛（0-10）：低于该值的论文视为「仅主题沾边、任务范式不同」，不进综述
PARADIGM_MIN_SCORE = 5.0
SURVEY_TEXTS_DIR = ROOT_DIR / "archive" / "survey_texts"
_REVIEW_INPUT_CHAR_CAP = 80000


def _paradigm_min_score() -> float:
    try:
        value = float(os.getenv("DPR_SURVEY_PARADIGM_MIN", "").strip())
    except ValueError:
        return PARADIGM_MIN_SCORE
    return value if value > 0 else PARADIGM_MIN_SCORE


class SurveyCancelled(Exception):
    """协作式取消信号：由调用方（Job 层）的 cancel_check 在需要取消时抛出。"""


def _log(message: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [survey] {message}", flush=True)


class _Ctx:
    """单次综述 run 的进度/取消/告警上下文，贯穿各阶段。"""

    def __init__(
        self,
        query: str,
        on_progress: Optional[Callable[..., None]] = None,
        cancel_check: Optional[Callable[[], None]] = None,
    ) -> None:
        self.query = query
        self.on_progress = on_progress
        self.cancel_check = cancel_check
        self.warnings: List[str] = []
        # 召回路对比统计（A/B 核心）：{lane: {latency_s, hits, papers_in_final, avg_relevance, ...}}
        self.lane_stats: Dict[str, Dict[str, Any]] = {}
        # 漏斗规模与护栏：{fts_candidates, embed_pool, rerank_in, final, lexical_coverage}
        self.funnel: Dict[str, Any] = {}
        # 召回池语义贴合度（粗排编码时顺带计算；观察指标，不做硬门——
        # bge 余弦好坏例差距仅 0.04 无判别力，硬门用词面覆盖率）
        self.recall_coherence: Optional[float] = None
        # 粗筛候选池画像（年份/类别分布摘要，注入全局分析补历史脉络）
        self.candidate_profile: str = ""
        # 种子锚定产物（define_task_paradigm 有种子时填充，供大纲/写作/装配消费）
        self.task_definition: str = ""
        self.input_boundary: str = ""
        self.dataset_names: List[str] = []
        self.non_arxiv_refs: List[Dict[str, Any]] = []

    def progress(self, stage: str, message: str, *, current: Optional[int] = None, total: Optional[int] = None) -> None:
        if self.on_progress:
            self.on_progress(stage, message, current=current, total=total)

    def check_cancel(self) -> None:
        if self.cancel_check:
            self.cancel_check()

    def warn(self, message: str) -> None:
        self.warnings.append(str(message))
        _log(f"[WARN] {message}")


# --------------------------------------------------------------------------- #
# 模块加载（Step 脚本文件名带点，常规 import 不可用，统一 importlib 按路径加载）
# --------------------------------------------------------------------------- #

_LOADED_STEP_MODULES: Dict[str, Any] = {}


def _load_step_module(filename: str, alias: str) -> Any:
    cached = _LOADED_STEP_MODULES.get(alias)
    if cached is not None:
        return cached
    path = SRC_DIR / filename
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载流水线模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    _LOADED_STEP_MODULES[alias] = module
    return module


_GENERATE_DOCS_MODULE: Any = None
_GENERATE_DOCS_LOCK = threading.Lock()


def _load_generate_docs_module() -> Any:
    global _GENERATE_DOCS_MODULE
    with _GENERATE_DOCS_LOCK:
        if _GENERATE_DOCS_MODULE is not None:
            return _GENERATE_DOCS_MODULE
        path = SRC_DIR / "6.generate_docs.py"
        spec = importlib.util.spec_from_file_location("dpr_generate_docs_for_survey", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载日报文档模块：{path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _GENERATE_DOCS_MODULE = module
        return module


def _load_repo_config() -> Dict[str, Any]:
    import yaml

    cfg_path = ROOT_DIR / "config.yaml"
    if not cfg_path.exists():
        return {}
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# --------------------------------------------------------------------------- #
# LLM 客户端
# --------------------------------------------------------------------------- #


def resolve_survey_model() -> str:
    return (os.getenv("DPR_SURVEY_MODEL") or "").strip() or resolve_llm_model()


def make_survey_client() -> DeepSeekClient:
    """每个并发任务各自构造一个 client，避免共享实例的 kwargs 竞争（仓库既有约定）。"""
    return DeepSeekClient(
        api_key=resolve_llm_api_key(),
        model=resolve_survey_model(),
        base_url=resolve_llm_base_url(),
    )


def _chat_text(client: DeepSeekClient, system: str, user: str, *, max_tokens: Optional[int] = None) -> str:
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    kwargs: Dict[str, Any] = {}
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    resp = client.chat(messages, **kwargs)
    return str(resp.get("content") or "").strip()


def _chat_structured(client: DeepSeekClient, system: str, user: str, schema_name: str, schema: Dict[str, Any]) -> Optional[dict]:
    resp = client.chat_structured(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        schema_name,
        schema,
    )
    if resp.get("parse_error") or not isinstance(resp.get("parsed"), dict):
        return None
    return resp["parsed"]


# --------------------------------------------------------------------------- #
# Prompt（移植自 paper_agent/src/core/prompts.py 与各 agent 内嵌 prompt，按结构化输出改造）
# --------------------------------------------------------------------------- #

_EXTRACT_SYSTEM = """
【角色定位】
你是学术信息抽取专家。请根据用户提供的单篇论文信息（标题 + 摘要），严格按 JSON schema 抽取并输出，
禁止编造原文未提及的信息，所有字段尽量使用原文短语或数值。

【任务范式判定（综述统一性关键）】
- task_paradigm：用一句英文短语概括该论文所属的任务范式（输入-输出形式 + 方法族，如
  "referring expression segmentation via multi-modal transformers"、"diffusion-based image generation"）；
- paradigm_consistency：0-10 分，衡量该论文与 target_task_paradigm 是否属于**同一任务范式或相近任务范式**
  （输入输出形式、要解决的问题类型、方法族是否一致）。同范式 8-10，相近 5-7，仅主题沾边但范式不同 2-4，无关 0-1。

【抽取要求】
- relevance：该论文与综述主题的相关度，0-10 分（10 = 高度核心）；
- core_problem：用"尽管…但…"或"为了…"句式概括核心问题；
- key_methodology.name：优先取原文给出的模型/算法/框架名；
- key_methodology.principle：1-2 句话描述技术路线（保留公式或缩写）；
- key_methodology.novelty：原文有"首次""我们提出"等表述直接引用，否则写"未明确声明"；
- datasets_used：数据集全称及规模（如 "SST-2 (67k sentences)"）；
- evaluation_metrics：仅保留与主实验直接相关的指标（如 Accuracy, F1, BLEU）；
- main_results：尽量带数值及对照基线（如 "在 IMDB 上 Accuracy 92.5%，优于 BERT 的 89.3%"）；
- limitations：原文自述的局限（如 "本研究仅考虑英语语料"）；
- contributions：3-5 条 bullet 式短语，保持原文时态。

信息缺失的字段用 null 或空列表，不要编造。仅返回 JSON object。
"""

_DEEP_ANALYSE_SYSTEM = """
你是一位专业的学术研究分析师，擅长从多篇相关论文中提取深度见解。请基于提供的聚类信息和详细论文内容，进行系统性的学术分析，并以清晰的结构化 Markdown 呈现分析结果。
# 分析维度
请从以下四个维度进行系统性分析：

## 1. 技术发展趋势
- 分析该研究方向的演进脉络
- 识别关键的技术转折点和里程碑
- 分析研究热度的变化趋势

## 2. 方法论对比
- 对比不同论文采用的核心方法和技术路线
- 分析各方法的创新点和理论依据
- 评估不同方法论的优缺点

## 3. 性能表现评估
- 在共同数据集或评估指标上的横向对比
- 识别性能最优的方法及其关键因素
- 分析不同方法在不同场景下的适用性

## 4. 局限性与挑战
- 总结该技术路线的共同局限性
- 识别尚未解决的关键问题
- 展望未来的改进方向和研究机会
"""

_GLOBAL_ANALYSE_SYSTEM = """
你是一名具备跨领域技术分析能力的专家，擅长基于多主题聚类数据进行全局整合分析，能够精准提炼技术关联、对比方法差异、预判发展趋势，且输出内容逻辑严谨、专业详实。

# 输出质量标准
1. 逻辑连贯性：各模块之间需形成呼应（如"局限性总结"需与"技术趋势总结"中的技术方向对应，"建议与展望"需针对"局限性"提出解决方案）；
2. 内容深度：避免表层描述，需深入分析背后的技术原理、市场逻辑、行业需求，如对比方法时不仅说明"是什么"，还需解释"为什么不同""适用场景差异的本质原因"；
3. 实用性：研究建议需具备可操作性，避免空泛表述（如不说"加强技术研发"，而说"建议科研机构重点突破 XX 技术的 XX 环节，可通过 XX 实验方法验证可行性"）；
4. 可读性：结构清晰，语言简洁专业，对复杂术语给出简要解释。

若聚类分析结果中存在信息冲突或模糊之处，需基于行业通用认知与技术发展规律进行合理推断，并注明"数据存在模糊性，此处基于 XX 逻辑推断"。
"""

_GLOBAL_ANALYSE_MODULES = """
# 全局分析核心模块要求（需逐项满足，输出为 Markdown，每模块一个二级标题）

## 1. 技术趋势总结
- 明确各主题间的技术交叉点（如技术依赖、协同应用场景）；
- 梳理整体技术发展脉络（如从基础技术到衍生应用的演进路径）；
- 标注关键技术节点（如推动多主题共同发展的核心技术突破）。

## 2. 方法对比
- 按主题分类提炼核心方法（含技术原理、实现路径）；
- 从效率、成本、适用场景、精度等维度横向对比不同方法；
- 总结各方法的技术优势与适用边界。

## 3. 应用领域分析
- 按行业 / 场景维度归类各主题的应用案例；
- 分析不同应用领域的需求差异对技术选择的影响；
- 标注高潜力应用领域（需结合当前落地效果与市场需求）。

## 4. 研究热点识别
- 提炼当前各主题中关注度较高的技术方向（需说明关注原因）；
- 预测未来 1-3 年的潜在研究热点（需给出依据）；
- 区分"短期热点"（如技术优化类）与"长期趋势"（如技术架构变革类）。

## 5. 局限性总结
- 归纳各技术路线的共性局限性（如数据依赖、算力需求、兼容性问题等）；
- 分析局限性产生的根本原因；
- 说明局限性对实际应用的影响。

## 6. 建议与展望
- 针对局限性提出具体研究建议（如技术突破方向、产业链完善措施等）；
- 给出不同主体（如科研机构、企业、政策制定者）的行动建议；
- 展望技术成熟后的应用前景。
"""

_OUTLINE_SYSTEM = """
您是一位专业的写作指导，擅长将复杂的写作拆分成结构清晰、逻辑连贯的写作子任务。

# 任务要求
请根据用户提供的综述需求（可能含种子论文锚定的任务定义）、全局分析和主题聚类结果，生成结构清晰、
逻辑连贯的写作大纲，每个小节满足：
1. 有明确的主题和范围；
2. 包含足够的细节描述（focus 字段），指导写作者完成该部分；
3. 保持适当的粒度，既不过于宽泛也不过于琐碎；
4. 符合逻辑顺序和文章结构（通常 5-9 个小节）。

# 簇标注规则（重要）
每个小节必须标注它覆盖的主题簇：
- heading：小节标题（中文）；
- focus：详细描述和写作要点；
- cluster_ids：该节覆盖的簇 ID 列表（整数数组）；
- all_clusters：true 表示覆盖全部簇（引言、挑战、结论等全局性小节）；
- required_table：该节必须输出 markdown 对比表时填 "datasets"（数据集盘点表）或 "methods"
  （方法对比表），普通小节留空串。

# 结构脚手架（必须满足，缺一不可）
- 第一节为「引言」，all_clusters=true；
- 第二节为「任务定义与研究现状」：依据种子任务定义（若有）界定输入/输出/子任务划分，
  辨析与相近任务的输入边界，并梳理本任务已有的原生工作（数据集、基线、代表结果），all_clusters=true；
- 必须包含「数据集与评测基准盘点」一节，required_table="datasets"，all_clusters=true；
- 必须包含「方法对比」一节，required_table="methods"，all_clusters=true；
- 必须包含「研究脉络/发展阶段」类小节（传统方法→学习范式→大模型时代的演进），all_clusters=true；
- 建议包含「挑战与开放问题」，每条挑战需说明现有工作如何缓解、哪些仍未解决，all_clusters=true；
- 最后以「结论与展望」类小节收尾，all_clusters=true；
- 中间可穿插主题小节（每簇一节或合并相近簇）；
- 所有簇都必须被至少一个小节覆盖。
"""

_WRITER_SYSTEM = """
您是一位专业的学术作者，负责根据提供的资料撰写高质量的综述章节内容，并对使用的资料进行引用，确保引用的准确性和完整性。

# 写作质量要求
1. 学术规范：使用客观、中立的学术语言，重要观点应有逻辑或依据支撑；
2. 内容严谨性：区分事实陈述与观点分析，对不确定的内容保持谨慎；
3. 篇幅：普通小节 400-900 字中文，信息密度优先于长度；
4. 横向对比（核心要求）：介绍任何方法/路线时，必须与相邻路线比较——各自适合什么场景、
   优缺点、适用边界；禁止只做概念罗列不做比较；
5. 间接类比必须显式标注：若某论断来自通用领域的证据（如通用文档基准、通用 3D 生成），
   必须写明「该结论来自 XX 通用领域，迁移到本任务时还需面对 …… 特有难点」，
   禁止把类比论证伪装成直接证据；
6. 引用须有信息量：挂 [n] 的同时用半句话提炼该文献的核心结论或数据
   （如「[3] 报告在 X 基准上将 Y 提升 12%」），禁止只挂编号不复述要点；
7. 标注为「种子直系」的论文是本任务的原生工作，任务定义/数据集/方法对比各节必须优先覆盖它们。

# 引用规则（最重要）
- 只能使用资料中给出的论文编号引用，格式为 [n]，多篇连用写作 [n1,n2]；
- 严禁引用编号之外的论文，严禁编造文献、数据、实验结果；
- 每段关键论断至少挂一处引用；
- 不要输出参考文献列表（由系统统一生成），不要重复小节标题。

# 论断必须有资料支撑（防幻觉铁律）
- 综述是对「所给文献集合」的总结：每个技术论断（方法原理、性能数字、领域现状、
  技术演进阶段划分）都必须能在所给论文资料中找到依据并挂 [n]；
- 禁止使用资料之外的领域通识填充正文——包括你记忆中的经典论文、方法名、
  基准名；资料里没有的内容宁可略去或写「所给文献未覆盖」，不得凭记忆补写；
- 若发现所给资料与综述主题明显不符（论文内容与主题是两个领域），不要强行
  围绕主题写作，改为在段首明确说明「本次候选文献与主题匹配度低」并如实
  总结资料实际内容，不得产出与参考文献脱节的正文。

# 表格要求
要求输出表格的小节（提示中会注明）必须给出规范的 markdown 表格（列名加粗、管道对齐），
表格之后再用 1-2 段文字做解读；资料不足的单元格写「未披露」，禁止编造。

只输出小节正文 Markdown（不要以标题开头）。
"""

_REVIEW_SYSTEM = """
你是一个专业的学术审查助手，负责对生成的综述报告草稿进行质量评估与修订。

# 审查维度
1. 符合性：报告是否完整回应了综述主题，结构是否完整（引言/任务定义/数据集盘点/方法对比/挑战/结论）；
2. 原生文献覆盖：任务定义与现状梳理是否覆盖了标注为「种子直系」的本任务原生工作；
3. 正文-引用对应性（重点抽查）：随机抽查 5 个技术论断（方法名/性能数字/现状判断），
   核对其引用的 [n] 文献是否真的支撑该论断；把无文献支撑、仅凭领域通识写出的
   段落删除或改为「所给文献未覆盖」，把挂错编号的引用改正；
4. 对比完整性：方法之间是否有实质性横向比较（优劣/适用边界），对比表是否完整；
5. 类比论证规范：来自通用领域的间接论据是否已显式标注来源领域与本任务特有差异；
6. 内容质量：分析是否准确、逻辑是否有漏洞或矛盾、观点是否客观中立；
7. 语言与规范：学术语言是否规范、表达是否清晰流畅、引用格式是否为 [n] 且复述了文献要点；
8. 学术伦理：引用是否恰当、是否注明局限性、不得编造内容。

# 修订要求
- 直接给出修订后的完整报告 Markdown（revised_markdown 字段），保持原有结构与引用编号；
- 修补章节间过渡、删除冗余重复表述、统一术语与引用格式；
- 为缺少横向对比的方法段补写比较，为未标注的类比论证补标注；
- 不新增引用编号，不篡改具体数据与结论；
- issues_found 字段列出发现的主要问题（每条一句话），无问题则给空列表。
"""


# --------------------------------------------------------------------------- #
# Stage 1: recall —— BM25 + 向量双路召回，RRF 融合
# --------------------------------------------------------------------------- #

# 召回时间窗越长，候选池上限越大：保证跨年级综述有足够的池子供精排挑选
MAX_FETCH_DAYS = 1095  # 3 年


def _adaptive_recall_top_k(fetch_days: int) -> int:
    if fetch_days > 180:
        return 500
    if fetch_days > 30:
        return 350
    return RECALL_TOP_K


def define_task_paradigm(
    ctx: "_Ctx",
    client_factory: Callable[[], DeepSeekClient],
    *,
    seed_analysis: Optional[Dict[str, Any]] = None,
) -> str:
    """确定目标任务范式定义，作为逐篇范式一致性判定的锚点。

    有种子论文时直接采用种子的范式定义（零 LLM 调用，且比拍主题脑袋归纳准得多），
    同时把任务定义/输入边界存入 ctx 供大纲阶段使用；失败返回空串——
    抽取阶段的范式门随后自动跳过（降级为仅 relevance 过滤）。
    """
    ctx.check_cancel()
    if seed_analysis and str(seed_analysis.get("target_paradigm") or "").strip():
        paradigm = str(seed_analysis["target_paradigm"]).strip()
        ctx.task_definition = str(seed_analysis.get("task_definition") or "").strip()
        ctx.input_boundary = str(seed_analysis.get("input_boundary") or "").strip()
        ctx.dataset_names = [str(d) for d in (seed_analysis.get("dataset_names") or [])][:20]
        ctx.non_arxiv_refs = (seed_analysis.get("non_arxiv_refs") or [])[:15]
        _log(f"目标范式（种子锚定）：{paradigm[:120]}")
        ctx.progress("extract", "任务范式已由种子论文锚定：" + paradigm[:80])
        return paradigm
    ctx.progress("extract", "归纳主题的目标任务范式（用于统一研究方向）")
    system = (
        "你是一位严谨的学术调研规划专家。给定一个综述主题，请先判定其核心任务范式，"
        "再输出一段 2-4 句的英文范式定义（target_task_paradigm），说明：输入-输出形式、"
        "要解决的问题类型、典型方法族。这个定义将作为筛选举报论文的硬性标尺："
        "只有属于同一任务范式或相近任务范式的论文才允许进入综述。只输出该定义本身，不要解释。"
    )
    try:
        definition = _chat_text(client_factory(), system, f"Survey topic: {ctx.query}")
    except Exception as exc:  # noqa: BLE001
        ctx.warn(f"任务范式归纳失败，本篇综述退化为仅按相关度过滤：{exc}")
        return ""
    definition = (definition or "").strip().strip('"')
    if definition:
        _log(f"目标范式：{definition[:120]}")
        ctx.progress("extract", "任务范式已确定：" + definition[:80])
    return definition


def _intent_query_text(query: str) -> str:
    return f"Find recent papers relevant to: {query}"


_QUERY_PLAN_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "queries": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["queries"],
}


def plan_recall_queries(
    ctx: "_Ctx",
    client_factory: Callable[[], DeepSeekClient],
) -> List[str]:
    """无种子时把任意语言主题规划成英文检索查询组。

    中文主题直查英文文献库会全军覆没（FTS 提不出英文词、英文 embedding 编码
    中文查询产生噪声向量 → 曾召回全部无关论文并产出主题漂移的幻觉综述），
    故无种子时先花 1 次 LLM 调用把主题翻成 5-8 条英文检索查询，驱动全部召回路。
    失败返回空列表，回退主题单查询（现状行为）。
    """
    ctx.check_cancel()
    ctx.progress("recall", "查询规划：将主题转写为英文检索查询组")
    system = (
        "You are an academic literature search planner. Given a survey topic in ANY language, "
        "produce 5-8 English search queries for retrieving relevant arXiv papers. "
        "Rules: each query is a short natural-language phrase (not keyword soup) describing one "
        "facet of the topic (core task, method families, benchmarks/datasets, or closely "
        "adjacent formulations); translate non-English topics into precise English terminology "
        "used in the literature; do NOT include stopwords-only filler; queries must be diverse "
        "but all clearly relevant to the topic. Return JSON {\"queries\": [...]}."
    )
    try:
        parsed = _chat_structured(
            client_factory(), system, f"Survey topic: {ctx.query}", "survey_query_plan", _QUERY_PLAN_SCHEMA
        )
    except Exception as exc:  # noqa: BLE001
        ctx.warn(f"查询规划失败，回退为主题单查询：{exc}")
        return []
    queries = [str(q).strip() for q in ((parsed or {}).get("queries") or []) if str(q).strip()]
    queries = queries[:8]
    if queries:
        _log(f"查询规划完成：{len(queries)} 条英文查询，首条：{queries[0][:80]}")
        ctx.progress("recall", f"查询规划完成：{len(queries)} 条英文检索查询")
    return queries


def _build_bm25_query(query: str) -> Dict[str, Any]:
    return {
        "type": "keyword",
        "tag": "survey",
        "paper_tag": "keyword:survey",
        "query_text": query,
        "paper_sources": ["arxiv"],
        "active_source": "arxiv",
    }


def _build_embedding_query(intent_text: str, query_embedding: Any) -> Dict[str, Any]:
    return {
        "type": "query",
        "tag": "survey",
        "paper_tag": "query:survey",
        "query_text": intent_text,
        "query_embedding": query_embedding,
        "paper_sources": ["arxiv"],
        "active_source": "arxiv",
    }


def _pdf_url_from_link(link: str, paper_id: str) -> str:
    link = (link or "").strip()
    if "/abs/" in link:
        return link.replace("/abs/", "/pdf/")
    if link:
        return link
    return f"https://arxiv.org/pdf/{paper_id}" if paper_id else ""


def _normalize_arxiv_id(pid: str) -> str:
    """剥离 arXiv 版本号（2608.19567v4 → 2608.19567）。

    召回去重、引文直取、参考文献编号全链统一用它——同一论文的不同版本
    不得占用多个候选/引用位（曾出现 Block3D v1-v4 占 4 个引用位的 bug）。
    """
    text = str(pid or "").strip()
    match = re.match(r"^(\d{4}\.\d{4,5})", text)
    if match:
        return match.group(1)
    return text


def _paper_to_dict(paper: Any) -> Dict[str, Any]:
    pid = _normalize_arxiv_id(str(getattr(paper, "id", "") or "").strip())
    link = str(getattr(paper, "link", "") or "").strip()
    return {
        "paper_id": pid,
        "title": str(getattr(paper, "title", "") or "").strip(),
        "abstract": str(getattr(paper, "abstract", "") or "").strip(),
        "authors": [str(a) for a in (getattr(paper, "authors", None) or [])],
        "published": str(getattr(paper, "published", "") or "").strip(),
        "link": link,
        "pdf_url": _pdf_url_from_link(link, pid),
        "source": str(getattr(paper, "source", "") or "arxiv").strip() or "arxiv",
    }


def _first_query_scores(result: Dict[str, Any]) -> Any:
    queries = result.get("queries") or []
    return queries[0].get("sim_scores") if queries else {}


_EMBED_MODEL_CACHE: Dict[Tuple[str, str], Any] = {}
_COARSE_EMBED_MODEL_CACHE: Any = None
_EMBED_MODEL_LOCK = threading.Lock()


def _load_embedding_model(
    *,
    remote_endpoint: str | None = None,
    remote_api_key: str | None = None,
) -> Any:
    cache_key = (str(remote_endpoint or ""), str(remote_api_key or ""))
    with _EMBED_MODEL_LOCK:
        if cache_key in _EMBED_MODEL_CACHE:
            return _EMBED_MODEL_CACHE[cache_key]
        from model_loader import load_sentence_transformer  # noqa: E402

        model = load_sentence_transformer(
            EMBED_MODEL_NAME,
            device=os.getenv("DPR_SURVEY_EMBED_DEVICE", "cpu"),
            remote_endpoint=remote_endpoint,
            remote_api_key=remote_api_key,
            log=_log,
        )
        _EMBED_MODEL_CACHE[cache_key] = model
        return model


def _load_coarse_embedding_model() -> Any:
    """粗排专用 embedding：强制本地直连（allow_remote=False）。

    万篇候选走远端 HTTP 会逐批往返（数量级变慢），粗排必须吃本地 GPU/CPU；
    与查询编码用的远端单例分开缓存，互不影响。
    """
    global _COARSE_EMBED_MODEL_CACHE
    with _EMBED_MODEL_LOCK:
        if _COARSE_EMBED_MODEL_CACHE is not None:
            return _COARSE_EMBED_MODEL_CACHE
        from model_loader import load_sentence_transformer  # noqa: E402

        model = load_sentence_transformer(
            EMBED_MODEL_NAME,
            device=os.getenv("DPR_SURVEY_EMBED_DEVICE", "cpu"),
            allow_remote=False,
            log=_log,
        )
        _COARSE_EMBED_MODEL_CACHE = model
        return model


# --------------------------------------------------------------------------- #
# 多源融合：各 lane 均为按相关度排序的论文 dict 列表，RRF 合分 + 归一化 id 去重
# --------------------------------------------------------------------------- #


def fuse_recall_pools(
    lanes: List[List[Dict[str, Any]]],
    pool_cap: int,
    *,
    rrf_k: int = RRF_K,
) -> List[Dict[str, Any]]:
    """多路候选融合：每路按名次贡献 1/(rrf_k+rank)，同名（归一化 id）论文合分去重。

    去重时以合分最高记录为基底，其余路的非空字段补缺（如 DeepXiv 的被引数），
    recall_sources 记录该论文出现在哪些路（local/deepxiv/seed_citation）。
    """
    scores: Dict[str, float] = {}
    records: Dict[str, Dict[str, Any]] = {}
    for lane in lanes:
        for rank, paper in enumerate(lane, start=1):
            pid = _normalize_arxiv_id(str(paper.get("paper_id") or "").strip())
            if not pid or not str(paper.get("title") or "").strip():
                continue
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (rrf_k + rank)
            base = records.get(pid)
            if base is None:
                records[pid] = dict(paper, paper_id=pid, recall_sources=[str(paper.get("source") or "local")])
                continue
            for key, value in paper.items():
                if key in ("paper_id", "rrf_score", "recall_sources"):
                    continue
                if value and not base.get(key):
                    base[key] = value
            if paper.get("citation_count"):
                base["citation_count"] = max(int(base.get("citation_count") or 0), int(paper["citation_count"]))
            src = str(paper.get("source") or "").strip()
            if src and src not in (base.get("recall_sources") or []):
                base.setdefault("recall_sources", []).append(src)

    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[: max(int(pool_cap or 1), 1)]
    fused: List[Dict[str, Any]] = []
    for pid, score in ordered:
        record = records.get(pid)
        if not record:
            continue
        record["rrf_score"] = round(float(score), 6)
        fused.append(record)
    return fused


def _local_recall_lane(
    ctx: _Ctx,
    queries: List[str],
    *,
    fetch_days: int,
    pool_cap: int,
    supabase_conf: Dict[str, Any],
    embedding_endpoint: str | None = None,
    embedding_api_key: str | None = None,
) -> List[Dict[str, Any]]:
    """本地 Supabase 召回（BM25+向量双路，多条查询逐条跑再 RRF 合分）。

    时间窗设默认上限：本地库只保留近期论文（retention 清理），超长窗口只会
    触发 statement-timeout(57014) 的二分重试风暴（5 年窗实测拖 7 分钟）；
    长回溯由 DeepXiv 外部路覆盖。可用 DPR_SURVEY_LOCAL_RECALL_MAX_DAYS 覆写。
    """
    local_max_days = int(os.getenv("DPR_SURVEY_LOCAL_RECALL_MAX_DAYS") or 0) or 180
    local_days = min(int(fetch_days or 1), max(int(local_max_days), 1))
    if local_days < int(fetch_days or 1):
        ctx.warn(f"本地库召回窗口截断为 {local_days} 天（库内仅存近期论文，长回溯由外部检索覆盖）")
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=local_days)

    model = _load_embedding_model(
        remote_endpoint=embedding_endpoint,
        remote_api_key=embedding_api_key,
    )
    from filter import encode_queries  # noqa: E402

    ctx.progress("recall", f"编码查询向量（{EMBED_MODEL_NAME}，{len(queries)} 条查询）")
    embeddings = encode_queries(model, queries)

    mod21 = _load_step_module("2.1.retrieval_papers_bm25.py", "dpr_survey_step_21")
    mod22 = _load_step_module("2.2.retrieval_papers_embedding.py", "dpr_survey_step_22")
    mod23 = _load_step_module("2.3.retrieval_papers_rrf.py", "dpr_survey_step_23")

    score_map: Dict[str, float] = {}
    papers_by_id: Dict[str, Dict[str, Any]] = {}
    for idx, query in enumerate(queries, start=1):
        ctx.progress("recall", f"本地库召回 {idx}/{len(queries)}：{query[:60]}")
        bm25_result = mod21.rank_papers_for_queries_via_supabase(
            [_build_bm25_query(query)],
            pool_cap,
            supabase_conf,
            start_dt=start_dt,
            end_dt=end_dt,
        )
        emb_result = mod22.rank_papers_for_queries_via_supabase(
            model,
            [_build_embedding_query(query, embeddings[idx - 1])],
            pool_cap,
            supabase_conf,
            start_dt=start_dt,
            end_dt=end_dt,
            rpc_mode="exact",
            rpc_name_override=str(supabase_conf.get("vector_rpc_exact") or supabase_conf.get("vector_rpc") or ""),
        )
        bm25_ranks = mod23.normalize_rank_list(_first_query_scores(bm25_result))
        emb_ranks = mod23.normalize_rank_list(_first_query_scores(emb_result))
        for pid, score in mod23.rrf_fuse(bm25_ranks, emb_ranks, RRF_K).items():
            score_map[pid] = score_map.get(pid, 0.0) + score
        for result in (bm25_result, emb_result):
            for pid, paper in (result.get("papers") or {}).items():
                if pid:
                    papers_by_id.setdefault(_normalize_arxiv_id(pid), _paper_to_dict(paper))

    ranked = sorted(score_map.items(), key=lambda kv: kv[1], reverse=True)
    lane: List[Dict[str, Any]] = []
    for pid, _score in ranked[:pool_cap]:
        record = papers_by_id.get(_normalize_arxiv_id(pid))
        if record:
            lane.append(record)
    return lane


def _deepxiv_recall_lane(
    ctx: _Ctx,
    queries: List[str],
    *,
    fetch_days: int,
    per_query_top_k: int = 30,
) -> List[Dict[str, Any]]:
    """DeepXiv 全 arXiv 语义检索（回溯日期窗）。失败抛 DeepXivError 由调用方降级。"""
    from deepxiv_client import DeepXivClient  # noqa: PLC0415

    client = DeepXivClient()
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start_date = (datetime.now(timezone.utc) - timedelta(days=max(int(fetch_days or 1), 1))).strftime("%Y-%m-%d")
    merged: Dict[str, Dict[str, Any]] = {}
    for idx, query in enumerate(queries, start=1):
        ctx.progress("recall", f"DeepXiv 外部检索 {idx}/{len(queries)}：{query[:60]}")
        hits = client.search(query, top_k=per_query_top_k, date_start=start_date, date_end=end_date)
        for rank, paper in enumerate(hits, start=1):
            pid = paper["paper_id"]
            if pid not in merged:
                merged[pid] = dict(paper, _lane_rank=rank)
                continue
            merged[pid]["_lane_rank"] = min(merged[pid]["_lane_rank"], rank)
    ordered = sorted(merged.values(), key=lambda p: p["_lane_rank"])
    for paper in ordered:
        paper.pop("_lane_rank", None)
    return ordered


def _kaggle_recall_lane(
    ctx: _Ctx,
    queries: List[str],
    *,
    fetch_days: int,
    coarse_top_k: int,
) -> List[Dict[str, Any]]:
    """Kaggle/Cornell arXiv 全量快照 FTS5 词法粗筛（250 万篇本地检索，零网络无限流）。

    快照覆盖全历史且无 statement-timeout 问题——回溯窗口不受本地库 180 天上限约束；
    周级滞后由 DeepXiv 路补位。索引未建时由调用方降级跳过。
    """
    from kaggle_arxiv import KaggleArxivIndex, extractable_terms  # noqa: PLC0415

    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start_date = (datetime.now(timezone.utc) - timedelta(days=max(int(fetch_days or 1), 1))).strftime("%Y-%m-%d")
    per_query = max(int(coarse_top_k) // max(len(queries), 1), 500)
    # FTS 只认英文词：纯中文主题提不出任何词时显式提示（静默 0 命中曾产出主题漂移综述）
    if queries and not any(extractable_terms(q) for q in queries):
        ctx.warn(
            "Kaggle 快照粗筛只支持英文检索词，当前查询组无可提取英文词（该路跳过）。"
            "请使用英文主题，或提供种子论文让流水线自动派生英文查询。"
        )
        return []
    merged: Dict[str, Dict[str, Any]] = {}
    with KaggleArxivIndex() as index:
        for idx, query in enumerate(queries, start=1):
            ctx.progress("recall", f"Kaggle 快照粗筛 {idx}/{len(queries)}：{query[:60]}")
            hits = index.search(query, top_k=per_query, date_start=start_date, date_end=end_date)
            for rank, paper in enumerate(hits, start=1):
                pid = _normalize_arxiv_id(str(paper.get("paper_id") or "").strip())
                if not pid:
                    continue
                if pid not in merged:
                    merged[pid] = dict(paper, paper_id=pid, _lane_rank=rank)
                else:
                    merged[pid]["_lane_rank"] = min(merged[pid]["_lane_rank"], rank)
    ordered = sorted(merged.values(), key=lambda p: p["_lane_rank"])
    for paper in ordered:
        paper.pop("_lane_rank", None)
    return ordered[: max(int(coarse_top_k), 1)]


def _coarse_embedding_text(paper: Dict[str, Any]) -> str:
    """粗排嵌入文本：与 Supabase 库内向量同构（passage: Title/Abstract），保证语义空间一致。"""
    title = str(paper.get("title") or "").strip()
    abstract = str(paper.get("abstract") or "").strip()
    if title and abstract:
        return f"passage: Title: {title}\n\nAbstract: {abstract}"
    return f"passage: Title: {title or abstract}"


def coarse_rank_papers(
    ctx: _Ctx,
    papers: List[Dict[str, Any]],
    queries: List[str],
    *,
    embed_pool: int,
) -> List[Dict[str, Any]]:
    """本地语义粗排：万级词法候选 → embed_pool（供 rerank 消化的池）。

    论文得分 = 对全部查询向量的最大余弦（与多查询召回精神一致）；失败降级为
    直接截断 RRF 序（综述不因粗排失败中断）。量不大时零成本直通。
    """
    ctx.check_cancel()
    embed_pool = max(int(embed_pool or DEFAULT_EMBED_POOL), 1)
    if len(papers) <= embed_pool:
        return papers
    ctx.progress("coarse", f"本地语义粗排：{len(papers)} 篇候选 → 收窄至 {embed_pool}")
    started = time.time()
    try:
        model = _load_coarse_embedding_model()
        from filter import encode_queries  # noqa: PLC0415

        query_texts = [str(q).strip() for q in (queries or []) if str(q).strip()] or [
            _intent_query_text(ctx.query)
        ]
        q_vecs = np.asarray(encode_queries(model, query_texts))
        texts = [_coarse_embedding_text(p) for p in papers]
        batch = 64 if str(getattr(model, "device", "")).startswith("cuda") else 32
        chunk = 2000
        vec_parts: List[np.ndarray] = []
        for i in range(0, len(texts), chunk):
            vec_parts.append(
                np.asarray(
                    model.encode(
                        texts[i : i + chunk],
                        convert_to_numpy=True,
                        normalize_embeddings=True,
                        batch_size=batch,
                        show_progress_bar=False,
                    )
                )
            )
            ctx.progress("coarse", f"语义编码 {min(i + chunk, len(texts))}/{len(texts)} 篇")
        p_vecs = np.concatenate(vec_parts, axis=0)
        scores = np.max(q_vecs @ p_vecs.T, axis=0)
        order = np.argsort(-scores)[:embed_pool]
        picked = [papers[int(i)] for i in order]
        # 语义贴合度（观察指标）：头部候选对查询向量的平均最大余弦
        ctx.recall_coherence = round(float(np.sort(scores)[-min(10, len(scores)):].mean()), 4)
        elapsed = time.time() - started
        ctx.progress("coarse", f"粗排完成：{len(papers)} → {len(picked)}（{elapsed:.0f}s，本地 bge 语义）")
        _log(f"粗排完成：{len(papers)} → {len(picked)}，耗时 {elapsed:.0f}s")
        return picked
    except Exception as exc:  # noqa: BLE001
        ctx.warn(f"语义粗排失败，降级为直接截断候选池前 {embed_pool} 篇：{exc}")
        return papers[:embed_pool]


def summarize_candidate_profile(papers: List[Dict[str, Any]], *, top_n: int = 600) -> str:
    """粗筛候选池画像：年份/类别分布摘要（注入全局分析，补「历史脉络」维度）。"""
    years: Counter = Counter()
    cats: Counter = Counter()
    for paper in papers[: max(int(top_n), 1)]:
        year = str(paper.get("published") or "")[:4]
        if re.match(r"^\d{4}$", year):
            years[year] += 1
        for cat in str(paper.get("categories") or "").split():
            cats[cat] += 1
    if not years:
        return ""
    years_line = "、".join(f"{y} 年 {n} 篇" for y, n in sorted(years.items()))
    cats_line = "、".join(f"{c}（{n}）" for c, n in cats.most_common(8))
    parts = [f"年份分布（候选池头部 {min(len(papers), top_n)} 篇）：{years_line}"]
    if cats_line:
        parts.append(f"主要类别：{cats_line}")
    return "；".join(parts)


def _aggregate_lane_stats(
    extractions: List[Dict[str, Any]],
    lane_stats: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """终稿按 recall_sources 聚合质量指标（DeepXiv vs Kaggle A/B 对比核心）。

    每路输出：召回侧 latency_s/hits + 终稿侧 papers_in_final/avg_relevance/
    avg_paradigm_consistency；未进终稿的路保留召回侧统计（对比表需要）。
    """
    per_source: Dict[str, Dict[str, Any]] = {}
    for record in extractions:
        for src in record.get("recall_sources") or []:
            entry = per_source.setdefault(src, {"papers_in_final": 0, "_rel": [], "_para": []})
            entry["papers_in_final"] += 1
            try:
                entry["_rel"].append(float(record.get("relevance") or 0.0))
            except (TypeError, ValueError):
                pass
            try:
                entry["_para"].append(float(record.get("paradigm_consistency")))
            except (TypeError, ValueError):
                pass
    out: Dict[str, Dict[str, Any]] = {}
    for src, entry in per_source.items():
        merged = {k: v for k, v in (lane_stats.get(src) or {}).items()}
        merged["papers_in_final"] = entry["papers_in_final"]
        if entry["_rel"]:
            merged["avg_relevance"] = round(sum(entry["_rel"]) / len(entry["_rel"]), 2)
        if entry["_para"]:
            merged["avg_paradigm_consistency"] = round(sum(entry["_para"]) / len(entry["_para"]), 2)
        out[src] = merged
    for src, stat in lane_stats.items():
        out.setdefault(src, dict(stat))
    return out


def recall_pool_lexical_coverage(papers: List[Dict[str, Any]], queries: List[str]) -> float:
    """候选池对查询组核心词的字面覆盖率（0-1，取最优查询）。

    融合池与主题错位的强判别信号：主题漂移场景（如中文主题召回全库无关论文）
    核心词几乎不在池内出现（实测≈0），正常召回（词法/语义命中）则普遍 >0.5。
    语义余弦不适合做该判据——bge 归一化向量跨文本基线余弦高达 0.7+，
    好坏例差距仅 0.04（实测），无判别力；词面信号两极分化。
    """
    from kaggle_arxiv import extractable_terms  # noqa: PLC0415

    term_sets: List[List[str]] = []
    for query in queries or []:
        terms = [t for t in extractable_terms(query)[:5] if len(t) >= 3]
        if terms:
            term_sets.append(terms)
    if not term_sets:
        return 0.0
    sample = papers[:300]
    if not sample:
        return 0.0
    best = 0.0
    for terms in term_sets:
        patterns = [re.compile(r"\b" + re.escape(t), re.IGNORECASE) for t in terms]
        hit = sum(
            1
            for p in sample
            if any(pat.search(f"{p.get('title', '')} {p.get('abstract', '')}") for pat in patterns)
        )
        best = max(best, hit / len(sample))
    return best


def recall_papers(
    ctx: _Ctx,
    *,
    fetch_days: int,
    top_k: int | None = None,
    queries: Optional[List[str]] = None,
    seed_citations: Optional[List[Dict[str, Any]]] = None,
    use_deepxiv: bool = False,
    use_kaggle: bool = False,
    coarse_top_k: Optional[int] = None,
    embedding_endpoint: str | None = None,
    embedding_api_key: str | None = None,
) -> List[Dict[str, Any]]:
    """多源召回融合：本地库 + DeepXiv 外部检索 + 种子引文直取 + Kaggle 快照粗筛。

    DeepXiv 默认关（外部服务有 token 限额/波动，需被引数/最新论文时显式开启）；
    Kaggle 快照路是默认主路。任一路失败/为空不致命（warn 后继续），各路全空才报错；
    Supabase 未配置时本地路跳过（此前会直接 raise，外部路引入后不再硬依赖）。
    Kaggle 路命中时融合池上限抬到粗筛量级（万级），由后续语义粗排收窄。
    """
    pool_cap = int(top_k or _adaptive_recall_top_k(int(fetch_days)))
    kaggle_budget = max(
        int(coarse_top_k or int(os.getenv("DPR_SURVEY_KAGGLE_COARSE_TOP_K") or 0) or DEFAULT_KAGGLE_COARSE_TOP_K),
        500,
    )
    query_list = [str(q).strip() for q in (queries or []) if str(q).strip()] or [
        _intent_query_text(ctx.query)
    ]

    lanes: List[List[Dict[str, Any]]] = []
    lane_tags: List[str] = []

    # 路 1：本地 Supabase（前 3 条查询，控 RPC 成本）
    # DPR_SURVEY_DISABLE_LOCAL_LANE=1 跳过本地路：Supabase 服务端慢（57014 二分风暴）
    # 或 A/B 对比需要隔离外部路变量时使用。
    config = _load_repo_config()
    supabase_conf = get_supabase_read_config(config)
    if str(os.getenv("DPR_SURVEY_DISABLE_LOCAL_LANE") or "").strip().lower() in {"1", "true", "yes", "on"}:
        ctx.warn("DPR_SURVEY_DISABLE_LOCAL_LANE=1：本地库召回路已按需跳过")
    elif supabase_conf.get("url") and supabase_conf.get("anon_key"):
        try:
            t0 = time.time()
            local_lane = _local_recall_lane(
                ctx,
                query_list[:3],
                fetch_days=fetch_days,
                pool_cap=pool_cap,
                supabase_conf=supabase_conf,
                embedding_endpoint=embedding_endpoint,
                embedding_api_key=embedding_api_key,
            )
            lanes.append(local_lane)
            lane_tags.append(f"本地库 {len(local_lane)}")
            ctx.lane_stats["local"] = {"latency_s": round(time.time() - t0, 1), "hits": len(local_lane)}
        except Exception as exc:  # noqa: BLE001
            ctx.warn(f"本地库召回失败（已跳过该路）：{exc}")
    else:
        ctx.warn("config.yaml 缺少 Supabase 读配置，本地库召回跳过")

    # 路 2：DeepXiv 外部检索（前 5 条查询；语义检索 + 被引数 + 周级新鲜度）
    if use_deepxiv:
        from deepxiv_client import DeepXivError, is_deepxiv_available  # noqa: PLC0415

        available, reason = is_deepxiv_available()
        if available:
            try:
                t0 = time.time()
                deepxiv_lane = _deepxiv_recall_lane(ctx, query_list[:5], fetch_days=fetch_days)
                lanes.append(deepxiv_lane)
                lane_tags.append(f"DeepXiv {len(deepxiv_lane)}")
                ctx.lane_stats["deepxiv"] = {"latency_s": round(time.time() - t0, 1), "hits": len(deepxiv_lane)}
            except Exception as exc:  # noqa: BLE001
                ctx.warn(f"DeepXiv 外部检索失败（已跳过该路）：{exc}")
        else:
            ctx.warn(reason)

    # 路 3：种子引文直取（按引文位置排序，种子背书）
    if seed_citations:
        lanes.append(list(seed_citations))
        lane_tags.append(f"种子引文 {len(seed_citations)}")
        ctx.lane_stats["seed_citation"] = {"latency_s": 0.0, "hits": len(seed_citations)}

    # 路 4：Kaggle/Cornell 快照 FTS 粗筛（前 5 条查询；万级词法宽筛主路）
    if use_kaggle:
        from kaggle_arxiv import is_kaggle_ready  # noqa: PLC0415

        ready, reason = is_kaggle_ready()
        if ready:
            try:
                t0 = time.time()
                kaggle_lane = _kaggle_recall_lane(ctx, query_list[:5], fetch_days=fetch_days, coarse_top_k=kaggle_budget)
                if kaggle_lane:
                    lanes.append(kaggle_lane)
                    lane_tags.append(f"Kaggle {len(kaggle_lane)}")
                    ctx.lane_stats["kaggle"] = {"latency_s": round(time.time() - t0, 1), "hits": len(kaggle_lane)}
            except Exception as exc:  # noqa: BLE001
                ctx.warn(f"Kaggle 快照粗筛失败（已跳过该路）：{exc}")
        else:
            ctx.warn(reason)

    # Kaggle 命中时融合池抬到粗筛量级；纯小池场景维持自适应上限
    fuse_cap = pool_cap
    if ctx.lane_stats.get("kaggle", {}).get("hits"):
        fuse_cap = max(pool_cap, kaggle_budget)
    papers = fuse_recall_pools(lanes, fuse_cap)
    ctx.funnel["fts_candidates"] = len(papers)
    _log(f"召回完成：{' / '.join(lane_tags) or '全部召回路为空'}，融合后候选池 {len(papers)} 篇")
    if not papers:
        raise RuntimeError("各路召回（本地库/DeepXiv/种子引文/Kaggle 快照）均未命中论文，请调整主题或回溯范围")
    ctx.progress("recall", f"召回完成，候选池 {len(papers)} 篇（{' / '.join(lane_tags)}）")
    return papers


# --------------------------------------------------------------------------- #
# Stage 2: rerank —— 复用 3.rank_papers 的 reranker，失败回退 RRF 序
# --------------------------------------------------------------------------- #


def _parse_rerank_results(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, dict):
        items = payload.get("results")
    else:
        items = payload
    if not isinstance(items, list):
        return []
    parsed: List[Dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict) and "index" in item:
            parsed.append({"index": int(item["index"]), "relevance_score": float(item.get("relevance_score") or 0.0)})
    parsed.sort(key=lambda x: x["relevance_score"], reverse=True)
    return parsed


def _build_reranker() -> Tuple[Any, str]:
    mod3 = _load_step_module("3.rank_papers.py", "dpr_survey_step_3")
    profile_config = mod3._resolve_rerank_profile_config(os.getenv("RERANK_PROFILE", ""))
    provider = mod3._normalize_rerank_provider(
        os.getenv("RERANK_PROVIDER") or profile_config.get("provider") or "public_zwwen"
    )
    rerank_model = (
        profile_config.get("model")
        or (os.getenv("LOCAL_RERANK_MODEL") if provider == "local" else os.getenv("RERANK_MODEL"))
        or getattr(mod3, "DEFAULT_LOCAL_RERANK_MODEL", "Qwen/Qwen3-Reranker-0.6B")
    )
    if provider == "local":
        reranker = mod3.LocalQwenReranker(
            model_name=rerank_model,
            device=os.getenv("DPR_SURVEY_RERANK_DEVICE", "cpu"),
            batch_size=4,
        )
    else:
        from reranker_api import SiliconFlowReranker  # noqa: E402

        api_key = mod3._resolve_remote_api_key(provider)
        base_url = mod3._resolve_remote_base_url(provider, profile_config)
        if not api_key or not base_url:
            raise RuntimeError("远端 reranker 缺少 API Key 或 Base URL")
        reranker = SiliconFlowReranker(api_key=api_key, base_url=base_url)
    return reranker, str(rerank_model)


def rerank_papers(ctx: _Ctx, papers: List[Dict[str, Any]], *, max_papers: int) -> List[Dict[str, Any]]:
    ctx.check_cancel()
    if len(papers) <= max_papers:
        return papers
    try:
        ctx.progress("rerank", f"Reranker 精选 {max_papers} 篇（候选 {len(papers)}）")
        reranker, rerank_model = _build_reranker()
        mod3 = _load_step_module("3.rank_papers.py", "dpr_survey_step_3")
        documents = [mod3.format_doc(p.get("title", ""), p.get("abstract", "")) for p in papers]
        payload = reranker.rerank(
            query=_intent_query_text(ctx.query),
            documents=documents,
            top_n=max_papers,
            model=rerank_model,
        )
        hits = _parse_rerank_results(payload)
        if not hits:
            raise RuntimeError("reranker 未返回有效结果")
        selected: List[Dict[str, Any]] = []
        for hit in hits[:max_papers]:
            idx = hit["index"]
            if 0 <= idx < len(papers):
                record = papers[idx]
                record["rerank_score"] = round(hit["relevance_score"], 6)
                selected.append(record)
        if not selected:
            raise RuntimeError("reranker 结果索引越界")
        _log(f"rerank 完成：{len(selected)} 篇入选")
        ctx.progress("rerank", f"精排完成，入选 {len(selected)} 篇")
        return selected
    except Exception as exc:  # noqa: BLE001
        ctx.warn(f"rerank 阶段失败，回退 RRF 序：{exc}")
        ctx.progress("rerank", f"rerank 失败已回退 RRF 序（{exc}）")
        return papers[:max_papers]


# --------------------------------------------------------------------------- #
# Stage 3: extract —— 逐篇结构化抽取（字段对齐 paper_agent reading_node）
# --------------------------------------------------------------------------- #

_EXTRACT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "relevance": {"type": "number"},
        "task_paradigm": {"type": "string"},
        "paradigm_consistency": {"type": "number"},
        "core_problem": {"type": "string"},
        "key_methodology": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "principle": {"type": "string"},
                "novelty": {"type": "string"},
            },
        },
        "datasets_used": {"type": "array", "items": {"type": "string"}},
        "evaluation_metrics": {"type": "array", "items": {"type": "string"}},
        "main_results": {"type": "string"},
        "limitations": {"type": "string"},
        "contributions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["relevance", "core_problem", "main_results"],
}


def _degraded_extraction(paper: Dict[str, Any], reason: str) -> Dict[str, Any]:
    abstract = (paper.get("abstract") or "").strip()
    return {
        "relevance": 5.0,
        "core_problem": abstract[:400],
        "key_methodology": {"name": "", "principle": "", "novelty": ""},
        "datasets_used": [],
        "evaluation_metrics": [],
        "main_results": "",
        "limitations": "",
        "contributions": [],
        "_degraded": True,
        "_degraded_reason": reason,
    }


def _extract_one(
    paper: Dict[str, Any],
    client_factory: Callable[[], DeepSeekClient],
    *,
    survey_topic: str = "",
    target_paradigm: str = "",
) -> Dict[str, Any]:
    client = client_factory()
    user = json.dumps(
        {
            "survey_topic": survey_topic,
            "target_task_paradigm": target_paradigm,
            "paper_id": paper.get("paper_id"),
            "title": paper.get("title"),
            "abstract": (paper.get("abstract") or "")[:4000],
        },
        ensure_ascii=False,
        indent=2,
    )
    parsed = _chat_structured(client, _EXTRACT_SYSTEM, user, "survey_paper_extraction", _EXTRACT_SCHEMA)
    if parsed is None:
        raise RuntimeError("抽取结构化输出解析失败")
    return parsed


def extract_papers(
    ctx: _Ctx,
    papers: List[Dict[str, Any]],
    *,
    client_factory: Callable[[], DeepSeekClient],
    concurrency: int = DEFAULT_EXTRACT_CONCURRENCY,
    survey_topic: str = "",
    target_paradigm: str = "",
) -> List[Dict[str, Any]]:
    ctx.check_cancel()
    total = len(papers)
    ctx.progress("extract", f"逐篇结构化抽取（{total} 篇，并发 {concurrency}）", current=0, total=total)
    results: Dict[int, Dict[str, Any]] = {}
    lock = threading.Lock()
    done = [0]

    def _work(idx: int, paper: Dict[str, Any]) -> None:
        record: Dict[str, Any]
        try:
            parsed = _extract_one(
                paper,
                client_factory,
                survey_topic=survey_topic or ctx.query,
                target_paradigm=target_paradigm,
            )
        except Exception as exc:  # noqa: BLE001
            parsed = _degraded_extraction(paper, str(exc))
        record = dict(paper)
        for key, value in parsed.items():
            if value is not None:
                record[key] = value
        with lock:
            results[idx] = record
            done[0] += 1
            ctx.progress("extract", f"已抽取 {done[0]}/{total}", current=done[0], total=total)

    with ThreadPoolExecutor(max_workers=max(int(concurrency), 1)) as pool:
        futures = [pool.submit(_work, idx, paper) for idx, paper in enumerate(papers)]
        for future in as_completed(futures):
            future.result()

    ordered = [results[idx] for idx in range(total) if idx in results]
    kept: List[Dict[str, Any]] = []
    dropped_relevance = 0
    dropped_paradigm = 0
    paradigm_min = _paradigm_min_score()
    for record in ordered:
        try:
            relevance = float(record.get("relevance") or 0.0)
        except (TypeError, ValueError):
            relevance = 0.0
        if relevance < RELEVANCE_MIN_SCORE:
            dropped_relevance += 1
            continue
        record["relevance"] = relevance
        if target_paradigm:
            # 任务范式门：与主题范式不同（仅主题沾边）的论文剔除，保证综述聚焦同一/相近任务范式
            try:
                consistency = float(record.get("paradigm_consistency"))
            except (TypeError, ValueError):
                consistency = None
            if consistency is not None and consistency < paradigm_min and not record.get("_degraded"):
                dropped_paradigm += 1
                ctx.warn(
                    f"范式过滤：剔除「{record.get('title', '')[:50]}」"
                    f"（范式一致性 {consistency:g}<{paradigm_min:g}，{record.get('task_paradigm') or '范式未知'}）"
                )
                continue
        kept.append(record)
    dropped = dropped_relevance + dropped_paradigm
    if dropped_relevance:
        ctx.warn(f"抽取后按 relevance≥{RELEVANCE_MIN_SCORE:g} 过滤掉 {dropped_relevance} 篇低相关论文")
    if dropped_paradigm:
        ctx.warn(f"抽取后按范式一致性≥{paradigm_min:g} 过滤掉 {dropped_paradigm} 篇跨范式论文")
    if not kept:
        raise RuntimeError("抽取后无符合相关度/任务范式的论文，请收窄主题或调整回溯范围")
    ctx.progress("extract", f"抽取完成：保留 {len(kept)} 篇（过滤低相关 {dropped_relevance}、跨范式 {dropped_paradigm}）")
    return kept


# --------------------------------------------------------------------------- #
# Stage 4: cluster —— 抽取字段拼接嵌入 + 肘部法则 KMeans + LLM 簇命名
# --------------------------------------------------------------------------- #


class _EmbedItem:
    """compute_embeddings 约定元素提供 text_for_embedding 属性。"""

    def __init__(self, text: str) -> None:
        self._text = text

    @property
    def text_for_embedding(self) -> str:
        return self._text


def cluster_embedding_text(extraction: Dict[str, Any]) -> str:
    """聚类嵌入文本：抽取字段拼接（paper_agent 设计），语义比原始摘要更贴近研究方向。"""
    parts: List[str] = []
    if extraction.get("core_problem"):
        parts.append(f"Problem: {extraction['core_problem']}")
    methodology = extraction.get("key_methodology") or {}
    if isinstance(methodology, dict) and methodology.get("name"):
        parts.append(f"Method: {methodology.get('name', '')} - {methodology.get('principle', '')}")
    if extraction.get("main_results"):
        results = extraction["main_results"]
        if isinstance(results, list):
            results = "; ".join(str(r) for r in results)
        parts.append(f"Results: {results}")
    if extraction.get("contributions"):
        parts.append("Contributions: " + "; ".join(str(c) for c in extraction["contributions"]))
    text = " ".join(parts).strip()
    if not text:
        text = f"{extraction.get('title', '')} {extraction.get('abstract', '')}".strip()
    return text


def determine_optimal_k(embeddings: Any, max_k: int = CLUSTER_MAX_K) -> int:
    """肘部法则：取 inertia 一阶差分最大处（paper_agent 移植）。"""
    n = len(embeddings)
    if n <= 2:
        return 1
    from sklearn.cluster import KMeans

    max_clusters = min(max_k, n - 1)
    if max_clusters <= 1:
        return 1
    inertias = []
    for k in range(1, max_clusters + 1):
        kmeans = KMeans(n_clusters=k, random_state=42, n_init=10)
        kmeans.fit(embeddings)
        inertias.append(kmeans.inertia_)
    if len(inertias) >= 3:
        differences = [inertias[i - 1] - inertias[i] for i in range(1, len(inertias))]
        return min(differences.index(max(differences)) + 2, max_clusters)
    return min(2, max_clusters)


def _kmeans_labels(embeddings: Any, k: int) -> List[int]:
    from sklearn.cluster import KMeans

    kmeans = KMeans(n_clusters=k, random_state=42, n_init=10)
    return [int(v) for v in kmeans.fit_predict(embeddings)]


def _fallback_labels(n: int, k: int) -> List[int]:
    return [i % k for i in range(n)]


_CLUSTER_NAMING_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "clusters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "cluster_id": {"type": "integer"},
                    "name_zh": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["cluster_id", "name_zh"],
            },
        }
    },
    "required": ["clusters"],
}


def cluster_papers(
    ctx: _Ctx,
    extractions: List[Dict[str, Any]],
    *,
    client_factory: Callable[[], DeepSeekClient],
) -> List[Dict[str, Any]]:
    ctx.check_cancel()
    n = len(extractions)
    texts = [cluster_embedding_text(e) for e in extractions]
    ctx.progress("cluster", f"论文向量编码（{n} 篇）")
    model = _load_embedding_model()
    from filter import compute_embeddings  # noqa: E402

    embeddings = compute_embeddings(model, [_EmbedItem(t) for t in texts])

    try:
        k = determine_optimal_k(embeddings)
        labels = _kmeans_labels(embeddings, k) if k > 1 else [0] * n
        method = f"kmeans(k={k})"
    except ImportError:
        k = max(min(3, n), 1)
        labels = _fallback_labels(n, k)
        method = f"order-partition(k={k})"
        ctx.warn("sklearn 不可用，聚类退化为按序等分")

    groups: Dict[int, List[int]] = {}
    for idx, label in enumerate(labels):
        groups.setdefault(label, []).append(idx)
    clusters = [
        {"cluster_id": cid, "paper_indices": idxs}
        for cid, idxs in sorted(groups.items(), key=lambda kv: min(kv[1]))
    ]

    # LLM 簇命名：每簇取前 3 篇的代表信息（paper_agent 限制前 3 篇的同样做法）
    naming_input = []
    for cluster in clusters:
        members = [
            {
                "title": extractions[i].get("title"),
                "problem": (extractions[i].get("core_problem") or "")[:200],
                "method": ((extractions[i].get("key_methodology") or {}).get("name") or ""),
            }
            for i in cluster["paper_indices"][:3]
        ]
        naming_input.append({"cluster_id": cluster["cluster_id"], "papers": members})
    naming_system = (
        "你是一个专业的学术研究助手，擅长从多篇论文中总结核心主题和关键词。"
        "请为每个簇生成简洁准确的主题描述和关键词：name_zh 为 20-30 字中文主题；keywords 为 3-5 个关键词。"
        "必须为每个簇给出非空主题，不能生成空主题。仅返回 JSON。"
    )
    try:
        parsed = _chat_structured(
            client_factory(),
            naming_system,
            json.dumps(naming_input, ensure_ascii=False),
            "survey_cluster_naming",
            _CLUSTER_NAMING_SCHEMA,
        )
        names = {int(item.get("cluster_id")): item for item in (parsed or {}).get("clusters", [])}
    except Exception as exc:  # noqa: BLE001
        names = {}
        ctx.warn(f"簇命名失败，使用默认主题名：{exc}")

    for cluster in clusters:
        info = names.get(cluster["cluster_id"]) or {}
        cluster["name_zh"] = str(info.get("name_zh") or f"主题方向 {cluster['cluster_id'] + 1}").strip()
        cluster["keywords"] = [str(kw) for kw in (info.get("keywords") or [])][:5]

    _log(f"聚类完成：{method}，{len(clusters)} 簇：" + "、".join(c["name_zh"] for c in clusters))
    ctx.progress("cluster", f"聚类完成（{method}）：{len(clusters)} 个主题簇")
    return clusters


# --------------------------------------------------------------------------- #
# Stage 5: deepread —— 每簇核心论文 PDF 全文（缓存复用）
# --------------------------------------------------------------------------- #


def _deep_read_targets(clusters: List[Dict[str, Any]], extractions: List[Dict[str, Any]]) -> List[int]:
    targets: List[int] = []
    for cluster in clusters:
        members = sorted(
            cluster["paper_indices"],
            key=lambda i: -float(extractions[i].get("relevance") or 0.0),
        )
        picked = 0
        for idx in members:
            if picked >= DEEP_READ_PER_CLUSTER:
                break
            if extractions[idx].get("full_text"):
                picked += 1
                continue
            if extractions[idx].get("pdf_url"):
                targets.append(idx)
                picked += 1
    return targets


def deep_read_core_papers(
    ctx: _Ctx,
    clusters: List[Dict[str, Any]],
    extractions: List[Dict[str, Any]],
    *,
    enabled: bool = True,
    deepxiv: Any = None,
) -> None:
    ctx.check_cancel()
    if not enabled:
        return
    targets = _deep_read_targets(clusters, extractions)
    if not targets:
        ctx.progress("deepread", "无需深读（无可用 PDF 或已有缓存）")
        return
    gd = _load_generate_docs_module()
    SURVEY_TEXTS_DIR.mkdir(parents=True, exist_ok=True)
    total = len(targets)
    ctx.progress("deepread", f"下载并抽取 {total} 篇核心论文全文", current=0, total=total)
    lock = threading.Lock()
    done = [0]

    def _work(idx: int) -> None:
        paper = extractions[idx]
        txt_path = SURVEY_TEXTS_DIR / f"{slugify(paper.get('paper_id') or paper.get('title') or 'paper')}.txt"
        text = ""
        # DeepXiv raw 优先：比 Jina/PDF 抓取稳定，失败再走既有链路（缓存命中时 ensure_text_content 直接返回）
        if deepxiv is not None and not txt_path.exists():
            try:
                text = (deepxiv.get_paper_markdown(str(paper.get("paper_id") or "")) or "").strip()
            except Exception:  # noqa: BLE001
                text = ""
        if not text:
            try:
                text = gd.ensure_text_content(str(paper.get("pdf_url") or ""), str(txt_path), fallback=paper.get("abstract") or "")
                text = (text or "").strip()
            except Exception as exc:  # noqa: BLE001
                text = ""
                ctx.warn(f"深读失败 {paper.get('paper_id')}：{exc}")
        if text:
            extractions[idx]["full_text"] = text[:DEEP_READ_TEXT_CHAR_CAP]
            if deepxiv is not None:
                try:
                    txt_path.parent.mkdir(parents=True, exist_ok=True)
                    txt_path.write_text(extractions[idx]["full_text"], encoding="utf-8")
                except Exception:  # noqa: BLE001
                    pass
        with lock:
            done[0] += 1
            ctx.progress("deepread", f"全文抽取 {done[0]}/{total}", current=done[0], total=total)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_work, idx) for idx in targets]
        for future in as_completed(futures):
            future.result()
    got = sum(1 for i in targets if extractions[i].get("full_text"))
    ctx.progress("deepread", f"深读完成：{got}/{total} 篇获得全文（缓存目录 {SURVEY_TEXTS_DIR.name}/）")


# --------------------------------------------------------------------------- #
# Stage 6: analyse —— 每簇深析（4 维度）+ 全局分析（6 模块）
# --------------------------------------------------------------------------- #


def _member_payload(extraction: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        "title": extraction.get("title"),
        "paper_id": extraction.get("paper_id"),
        "year": (extraction.get("published") or "")[:4] or None,
        "problem": extraction.get("core_problem"),
        "method": extraction.get("key_methodology"),
        "datasets": extraction.get("datasets_used"),
        "metrics": extraction.get("evaluation_metrics"),
        "results": extraction.get("main_results"),
        "limitations": extraction.get("limitations"),
        "contributions": extraction.get("contributions"),
        "relevance": extraction.get("relevance"),
    }
    if extraction.get("full_text"):
        payload["full_text_excerpt"] = extraction["full_text"][:6000]
    return payload


def analyse_clusters(
    ctx: _Ctx,
    clusters: List[Dict[str, Any]],
    extractions: List[Dict[str, Any]],
    *,
    client_factory: Callable[[], DeepSeekClient],
    concurrency: int = DEFAULT_WRITE_CONCURRENCY,
) -> Tuple[List[Dict[str, Any]], str]:
    ctx.check_cancel()
    total = len(clusters)
    ctx.progress("analyse", f"逐主题簇深入分析（{total} 簇，并发 {concurrency}）", current=0, total=total)
    results: Dict[int, Dict[str, Any]] = {}
    lock = threading.Lock()
    done = [0]

    def _work(cluster: Dict[str, Any]) -> None:
        members = [_member_payload(extractions[i]) for i in cluster["paper_indices"]]
        user = (
            f"## 基本信息\n- 聚类主题：{cluster['name_zh']}\n- 核心关键词：{', '.join(cluster.get('keywords') or [])}\n"
            f"- 论文数量：{len(members)}\n\n## 详细论文数据\n"
            + json.dumps(members, ensure_ascii=False, indent=2)
        )
        try:
            analysis = _chat_text(client_factory(), _DEEP_ANALYSE_SYSTEM, user)
        except Exception as exc:  # noqa: BLE001
            analysis = f"（该簇分析失败：{exc}）"
            ctx.warn(f"簇 {cluster['cluster_id']} 深析失败：{exc}")
        with lock:
            results[cluster["cluster_id"]] = {
                "cluster_id": cluster["cluster_id"],
                "theme": cluster["name_zh"],
                "keywords": cluster.get("keywords") or [],
                "paper_count": len(members),
                "analysis": analysis,
            }
            done[0] += 1
            ctx.progress("analyse", f"簇深析 {done[0]}/{total}", current=done[0], total=total)

    with ThreadPoolExecutor(max_workers=max(int(concurrency), 1)) as pool:
        futures = [pool.submit(_work, cluster) for cluster in clusters]
        for future in as_completed(futures):
            future.result()

    cluster_analyses = [results[c["cluster_id"]] for c in clusters if c["cluster_id"] in results]

    ctx.progress("analyse", "生成全局分析（6 模块）")
    summaries = [
        {
            "cluster_id": item["cluster_id"],
            "theme": item["theme"],
            "keywords": item["keywords"],
            "paper_count": item["paper_count"],
            "analyse_summary": item["analysis"][:1500],
        }
        for item in cluster_analyses
    ]
    profile_note = (
        f"\n（参考：粗筛候选池统计——{ctx.candidate_profile}。撰写「研究脉络与发展阶段」时"
        "请结合该年份分布判断各阶段的工作密度，避免只盯着最近一年的论文。）\n"
        if ctx.candidate_profile
        else ""
    )
    user = (
        "基于以下多主题聚类分析结果（JSON 数据），生成一份逻辑严谨、内容详实的全局分析。\n"
        + json.dumps(summaries, ensure_ascii=False, indent=2)
        + "\n"
        + profile_note
        + _GLOBAL_ANALYSE_MODULES
    )
    try:
        global_analysis = _chat_text(client_factory(), _GLOBAL_ANALYSE_SYSTEM, user)
    except Exception as exc:  # noqa: BLE001
        global_analysis = ""
        ctx.warn(f"全局分析失败：{exc}")
    if not global_analysis and cluster_analyses:
        global_analysis = "\n\n".join(f"## {item['theme']}\n\n{item['analysis']}" for item in cluster_analyses)
    ctx.progress("analyse", "全局分析完成")
    return cluster_analyses, global_analysis


# --------------------------------------------------------------------------- #
# Stage 7: outline —— 导演大纲（每节标注覆盖簇）
# --------------------------------------------------------------------------- #

_OUTLINE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "title_zh": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "focus": {"type": "string"},
                    "cluster_ids": {"type": "array", "items": {"type": "integer"}},
                    "all_clusters": {"type": "boolean"},
                    "required_table": {"type": "string"},
                },
                "required": ["heading", "focus"],
            },
        },
    },
    "required": ["title_zh", "sections"],
}


def build_outline(
    ctx: _Ctx,
    *,
    global_analysis: str,
    clusters: List[Dict[str, Any]],
    client_factory: Callable[[], DeepSeekClient],
) -> Dict[str, Any]:
    ctx.check_cancel()
    ctx.progress("outline", "生成报告大纲（导演模式，每节标注覆盖簇）")
    cluster_lines = [
        (
            f"  - 簇 {c['cluster_id']}: 主题=\"{c['name_zh']}\", "
            f"关键词=[{', '.join((c.get('keywords') or [])[:5])}], 论文数={len(c['paper_indices'])}"
        )
        for c in clusters
    ]
    seed_context = ""
    if ctx.task_definition:
        seed_context = (
            f"种子论文锚定的任务定义：\n{ctx.task_definition}\n\n"
            f"与相近任务的输入边界辨析：\n{ctx.input_boundary or '（无）'}\n\n"
        )
    dataset_hint = f"已知数据集线索：{', '.join(ctx.dataset_names)}\n\n" if ctx.dataset_names else ""
    user = (
        f"用户的需求:\n{ctx.query}\n\n"
        f"{seed_context}{dataset_hint}该领域的全局分析:\n{(global_analysis or '')[:6000]}\n\n"
        f"该领域的主题聚类结果（每个簇代表一组研究方向相近的论文）:\n" + "\n".join(cluster_lines) + "\n\n"
        "请生成写作大纲。返回 JSON：title_zh 为报告中文标题；sections 为小节数组，"
        "每节含 heading（中文标题）、focus（写作要点）、cluster_ids（覆盖的簇 ID 整数列表）、"
        "all_clusters（是否覆盖全部簇，布尔值）、required_table（该节必须输出 markdown 对比表时填 "
        "\"datasets\" 或 \"methods\"，否则空串）。"
    )
    try:
        parsed = _chat_structured(client_factory(), _OUTLINE_SYSTEM, user, "survey_outline", _OUTLINE_SCHEMA)
    except Exception as exc:  # noqa: BLE001
        parsed = None
        ctx.warn(f"大纲生成失败，使用默认骨架：{exc}")
    if not parsed or not parsed.get("sections"):
        parsed = {
            "title_zh": f"{ctx.query} 研究综述",
            "sections": [{"heading": "引言", "focus": "研究背景与意义", "cluster_ids": [], "all_clusters": True}]
            + [
                {"heading": c["name_zh"], "focus": f"围绕{c['name_zh']}主题的方法、结果与对比", "cluster_ids": [c["cluster_id"]], "all_clusters": False}
                for c in clusters
            ]
            + [{"heading": "结论与展望", "focus": "总结与未来方向", "cluster_ids": [], "all_clusters": True}],
        }

    title_zh = str(parsed.get("title_zh") or f"{ctx.query} 研究综述").strip()
    known_ids = {c["cluster_id"] for c in clusters}
    sections: List[Dict[str, Any]] = []
    for raw in parsed.get("sections") or []:
        heading = str(raw.get("heading") or "").strip()
        if not heading:
            continue
        cluster_ids = [int(v) for v in (raw.get("cluster_ids") or []) if str(v).lstrip("-").isdigit()]
        cluster_ids = [v for v in cluster_ids if v in known_ids]
        all_clusters = bool(raw.get("all_clusters")) or not cluster_ids
        required_table = str(raw.get("required_table") or "").strip()
        if required_table not in ("datasets", "methods"):
            required_table = ""
        sections.append(
            {
                "heading": heading,
                "focus": str(raw.get("focus") or "").strip(),
                "cluster_ids": cluster_ids,
                "all_clusters": all_clusters,
                "required_table": required_table,
            }
        )
    if not sections:
        raise RuntimeError("大纲没有任何有效小节")

    # 脚手架兜底 1：任务定义节必居第二位（引言之后）——综述读者首先要看到任务是什么
    if not any(("任务定义" in s["heading"] or "研究现状" in s["heading"] or "问题定义" in s["heading"]) for s in sections):
        task_focus = ctx.task_definition or f"界定{ctx.query}的任务输入输出与子任务划分"
        if ctx.input_boundary:
            task_focus += f"；辨析输入边界（{ctx.input_boundary[:120]}）"
        sections.insert(
            1,
            {
                "heading": "任务定义与研究现状",
                "focus": task_focus + "；梳理本任务已有的原生工作（数据集、基线方法与代表性结果）",
                "cluster_ids": [],
                "all_clusters": True,
                "required_table": "",
            },
        )
    # 脚手架兜底 2：数据集盘点表、方法对比表两节必须存在（required_table 标记）
    if not any(s.get("required_table") == "datasets" for s in sections):
        sections.append(
            {
                "heading": "数据集与评测基准盘点",
                "focus": "以 markdown 表格盘点本任务公开数据集：名称/规模/输入形式/标注内容/评测指标"
                + (f"；已知线索：{', '.join(ctx.dataset_names[:8])}" if ctx.dataset_names else ""),
                "cluster_ids": [],
                "all_clusters": True,
                "required_table": "datasets",
            }
        )
    if not any(s.get("required_table") == "methods" for s in sections):
        sections.append(
            {
                "heading": "方法对比：技术路线与适用边界",
                "focus": "以 markdown 表格横向对比不同技术路线（方法/输入形式/数据集/核心优势/局限与适用边界），"
                "表后逐条展开分析各路线间的优劣关系",
                "cluster_ids": [],
                "all_clusters": True,
                "required_table": "methods",
            }
        )
    # 脚手架兜底 3：引言/结论存在、每个簇都被覆盖
    if "引言" not in sections[0]["heading"]:
        sections.insert(0, {"heading": "引言", "focus": f"{ctx.query} 的研究背景、意义与综述结构", "cluster_ids": [], "all_clusters": True, "required_table": ""})
    if not any(("结论" in s["heading"] or "展望" in s["heading"]) for s in sections):
        sections.append({"heading": "结论与展望", "focus": "总结主要发现并展望未来研究方向", "cluster_ids": [], "all_clusters": True, "required_table": ""})
    covered = {cid for s in sections for cid in s["cluster_ids"]}
    for cluster in clusters:
        if cluster["cluster_id"] not in covered:
            sections.insert(
                max(len(sections) - 1, 1),
                {
                    "heading": cluster["name_zh"],
                    "focus": f"围绕{cluster['name_zh']}主题的方法、结果与对比",
                    "cluster_ids": [cluster["cluster_id"]],
                    "all_clusters": False,
                    "required_table": "",
                },
            )
    outline = {"title_zh": title_zh, "sections": sections}
    _log(f"大纲完成：{len(sections)} 节")
    ctx.progress("outline", f"大纲完成（{len(sections)} 节）")
    return outline


# --------------------------------------------------------------------------- #
# Stage 8: write —— 分节并行写作（并发闸门）+ 引用校验
# --------------------------------------------------------------------------- #

_CITATION_RE = re.compile(r"\[([\d,\s，\-]+)\]")


def sanitize_citations(markdown: str, max_ref: int) -> Tuple[str, int]:
    """校验 [n] 引用编号合法性：非法编号剔除，整组无效则删除该括号。返回 (正文, 剔除数)。"""
    removed = 0

    def _replace(match: "re.Match[str]") -> str:
        nonlocal removed
        inner = match.group(1)
        tokens = [t.strip() for t in re.split(r"[,，]", inner) if t.strip()]
        valid: List[str] = []
        for token in tokens:
            if "-" in token:
                lo, _, hi = token.partition("-")
                if lo.strip().isdigit() and hi.strip().isdigit() and 1 <= int(lo) <= int(hi) <= max_ref:
                    valid.append(token)
                    continue
                removed += 1
                continue
            if token.isdigit() and 1 <= int(token) <= max_ref:
                valid.append(token)
            else:
                removed += 1
        if not valid:
            return ""
        return "[" + ",".join(valid) + "]"

    return _CITATION_RE.sub(_replace, markdown), removed


def _paper_digest(index: int, extraction: Dict[str, Any]) -> str:
    method = extraction.get("key_methodology") or {}
    parts = [f"[{index}] {extraction.get('title', '')} ({(extraction.get('published') or '')[:4]})"]
    # 种子直系标注：引文直取来源的论文是本任务原生工作，任务定义/对比各节须优先覆盖
    sources = extraction.get("recall_sources") or []
    if "seed_citation" in sources:
        parts[0] += " ★种子直系文献"
    citations = extraction.get("citation_count") or 0
    if citations:
        parts[0] += f"（被引 {citations}）"
    if isinstance(method, dict) and method.get("name"):
        parts.append(f"    方法：{method.get('name')}——{(method.get('principle') or '')[:160]}")
    if extraction.get("main_results"):
        parts.append(f"    结果：{str(extraction['main_results'])[:220]}")
    if extraction.get("limitations"):
        parts.append(f"    局限：{str(extraction['limitations'])[:140]}")
    return "\n".join(parts)


def write_sections(
    ctx: _Ctx,
    *,
    outline: Dict[str, Any],
    clusters: List[Dict[str, Any]],
    cluster_analyses: List[Dict[str, Any]],
    global_analysis: str,
    extractions: List[Dict[str, Any]],
    client_factory: Callable[[], DeepSeekClient],
    concurrency: int = DEFAULT_WRITE_CONCURRENCY,
) -> Tuple[str, List[str]]:
    ctx.check_cancel()
    sections = outline["sections"]
    total = len(sections)
    ctx.progress("write", f"分节并行写作（{total} 节，并发 {concurrency}）", current=0, total=total)
    analysis_by_cluster = {item["cluster_id"]: item for item in cluster_analyses}
    results: Dict[int, str] = {}
    lock = threading.Lock()
    done = [0]

    def _work(idx: int, section: Dict[str, Any]) -> None:
        if section["all_clusters"]:
            member_indices = list(range(len(extractions)))
            cluster_context = "\n\n".join(
                f"### 簇分析：{item['theme']}\n{item['analysis'][:2500]}" for item in cluster_analyses
            )
        else:
            member_indices = sorted(
                {i for c in clusters if c["cluster_id"] in section["cluster_ids"] for i in c["paper_indices"]}
            )
            cluster_context = "\n\n".join(
                f"### 簇分析：{analysis_by_cluster[cid]['theme']}\n{analysis_by_cluster[cid]['analysis'][:3500]}"
                for cid in section["cluster_ids"]
                if cid in analysis_by_cluster
            )
        digests = "\n".join(_paper_digest(i + 1, extractions[i]) for i in member_indices)
        table_instruction = ""
        if section.get("required_table") == "datasets":
            table_instruction = (
                "\n\n【表格要求】本节必须输出数据集盘点 markdown 表格，"
                "列建议：数据集 | 年份 | 规模 | 输入形式 | 标注内容 | 评测指标；资料不足写「未披露」。"
            )
        elif section.get("required_table") == "methods":
            table_instruction = (
                "\n\n【表格要求】本节必须输出方法对比 markdown 表格，"
                "列建议：方法/路线 | 技术路线 | 输入形式 | 数据集 | 核心优势 | 局限与适用边界；"
                "表后逐条展开各路线间的优劣关系分析。"
            )
        seed_note = ""
        if ctx.task_definition:
            seed_note = f"种子锚定的任务定义（本综述聚焦该任务）：{ctx.task_definition[:800]}\n\n"
        user = (
            f"用户的综述主题：{ctx.query}\n\n{seed_note}"
            f"当前写作小节：{section['heading']}\n写作要点：{section['focus'] or '（无）'}{table_instruction}\n\n"
            f"领域全局分析（参考）：\n{(global_analysis or '')[:4000]}\n\n"
            f"本节相关主题簇分析：\n{cluster_context or '（无）'}\n\n"
            f"本节可引用的论文资料（只能引用这些编号；★种子直系文献为本任务原生工作须优先覆盖）：\n{digests}\n\n"
            "请撰写本小节正文。"
        )
        content = _chat_text(client_factory(), _WRITER_SYSTEM, user)
        with lock:
            results[idx] = content
            done[0] += 1
            ctx.progress("write", f"小节完成 {done[0]}/{total}（{section['heading']}）", current=done[0], total=total)

    with ThreadPoolExecutor(max_workers=max(int(concurrency), 1)) as pool:
        futures = [pool.submit(_work, idx, section) for idx, section in enumerate(sections)]
        for future in as_completed(futures):
            future.result()

    section_markdowns: List[str] = []
    total_removed = 0
    for idx, section in enumerate(sections):
        content = (results.get(idx) or "").strip()
        if not content:
            content = f"（本节写作失败：{section['heading']}）"
            ctx.warn(f"小节写作失败：{section['heading']}")
        content, removed = sanitize_citations(content, len(extractions))
        total_removed += removed
        section_markdowns.append(f"## {section['heading']}\n\n{content}")
    if total_removed:
        ctx.warn(f"写作阶段剔除 {total_removed} 处非法引用编号")
    ctx.progress("write", f"写作完成（剔除非法引用 {total_removed} 处）")
    return section_markdowns, sections


# --------------------------------------------------------------------------- #
# Stage 9: review —— 整体审校（失败用原稿）
# --------------------------------------------------------------------------- #

_REVIEW_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "revised_markdown": {"type": "string"},
        "issues_found": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["revised_markdown"],
}


def review_draft(
    ctx: _Ctx,
    draft: str,
    *,
    client_factory: Callable[[], DeepSeekClient],
) -> Tuple[str, List[str]]:
    ctx.check_cancel()
    if len(draft) > _REVIEW_INPUT_CHAR_CAP:
        ctx.warn(f"草稿超过 {_REVIEW_INPUT_CHAR_CAP} 字符，跳过整体审校")
        return draft, []
    ctx.progress("review", "整体审校与修订中")
    user = f"综述报告草稿如下，请按审查维度修订：\n\n{draft}"
    try:
        parsed = _chat_structured(client_factory(), _REVIEW_SYSTEM, user, "survey_review", _REVIEW_SCHEMA)
    except Exception as exc:  # noqa: BLE001
        parsed = None
        ctx.warn(f"审校失败，使用原稿：{exc}")
    revised = str((parsed or {}).get("revised_markdown") or "").strip()
    issues = [str(i) for i in ((parsed or {}).get("issues_found") or [])]
    if not revised:
        return draft, []
    ctx.progress("review", f"审校完成（发现 {len(issues)} 个问题）")
    return revised, issues


# --------------------------------------------------------------------------- #
# 报告装配
# --------------------------------------------------------------------------- #


def assemble_report(
    *,
    query: str,
    title_zh: str,
    section_markdowns: List[str],
    extractions: List[Dict[str, Any]],
    clusters: List[Dict[str, Any]],
    generated_at: str,
    non_arxiv_refs: Optional[List[Dict[str, Any]]] = None,
) -> str:
    lines: List[str] = [f"# {title_zh}", ""]
    lines.append(f"> 综述主题：{query}  ")
    cluster_line = "、".join(f"{c['name_zh']}（{len(c['paper_indices'])} 篇）" for c in clusters)
    lines.append(f"> 论文 {len(extractions)} 篇 · 主题簇 {len(clusters)} 个 · 生成于 {generated_at}  ")
    if cluster_line:
        lines.append(f"> {cluster_line}")
    lines += ["", "---", ""]
    for md in section_markdowns:
        lines += [md, ""]
    lines += ["## 参考文献", ""]
    for i, paper in enumerate(extractions, 1):
        link = (paper.get("link") or "").strip()
        # 被引数标注（DeepXiv 提供时）：帮助读者识别经典工作与新兴跟进
        cited = int(paper.get("citation_count") or 0)
        citation_note = f"（被引 {cited}）" if cited else ""
        seed_note = " ★" if "seed_citation" in (paper.get("recall_sources") or []) else ""
        lines.append(f"[{i}]{seed_note} {paper.get('title', '')}{citation_note}" + (f" — {link}" if link else ""))
    refs = [r for r in (non_arxiv_refs or []) if isinstance(r, dict) and str(r.get("title") or "").strip()]
    if refs:
        lines += ["", "## 延伸阅读（非 arXiv 文献）", ""]
        lines.append("以下文献来自种子论文参考文献中的非 arXiv 出版物（会议/期刊经典工作），供深入追溯：")
        lines.append("")
        for ref in refs[:15]:
            venue = str(ref.get("venue") or "").strip()
            year = str(ref.get("year") or "").strip()
            meta = "，".join(x for x in (venue, year) if x)
            lines.append(f"- {ref['title']}" + (f"（{meta}）" if meta else ""))
    return "\n".join(lines).strip() + "\n"


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #


def run_survey(
    query: str,
    *,
    max_papers: int = DEFAULT_MAX_PAPERS,
    fetch_days: int = DEFAULT_FETCH_DAYS,
    use_rerank: bool = True,
    deep_read: bool = True,
    seed_paper: Optional[Dict[str, Any]] = None,
    use_deepxiv: bool = False,
    use_kaggle: bool = False,
    coarse_top_k: Optional[int] = None,
    embedding_endpoint: str | None = None,
    embedding_api_key: str | None = None,
    on_progress: Optional[Callable[..., None]] = None,
    cancel_check: Optional[Callable[[], None]] = None,
    client_factory: Optional[Callable[[], DeepSeekClient]] = None,
) -> Dict[str, Any]:
    """执行一次完整综述流水线，返回结果 dict（供 survey_docs 落盘）。

    seed_paper：可选种子论文（{"arxiv_id"| "url"} 或 PDF 预抽的 {"text","title"}），
    用于锚定任务范式、派生检索查询并直取其参考文献（滚雪球式补齐原生文献）。
    """
    query = str(query or "").strip()
    if not query:
        raise ValueError("综述主题（query）不能为空")
    factory = client_factory or make_survey_client
    ctx = _Ctx(query, on_progress=on_progress, cancel_check=cancel_check)
    started = datetime.now(timezone.utc)

    # ---- 种子阶段：抓全文 → LLM 分析（任务定义/查询组/引文）→ 引文直取 ----
    seed_analysis: Optional[Dict[str, Any]] = None
    seed_citations: List[Dict[str, Any]] = []
    deepxiv_client_obj: Any = None
    if seed_paper:
        from survey_seed import analyze_seed, fetch_citation_papers, fetch_seed_text  # noqa: PLC0415

        if use_deepxiv:
            from deepxiv_client import DeepXivClient  # noqa: PLC0415

            try:
                deepxiv_client_obj = DeepXivClient()
            except Exception as exc:  # noqa: BLE001
                ctx.warn(f"DeepXiv 客户端不可用（种子全文与引文富化将走兜底链路）：{exc}")
        ctx.progress("seed", "抓取种子论文全文")
        seed_text = fetch_seed_text(seed_paper, deepxiv=deepxiv_client_obj, log=_log)
        ctx.progress("seed", f"种子分析中：{seed_text.get('title') or seed_text.get('arxiv_id')}"[:90])
        seed_analysis = analyze_seed(seed_text, factory)
        if not seed_analysis:
            ctx.warn("种子分析失败，本次综述退化为无种子模式（主题归纳范式）")
        else:
            n_queries = len(seed_analysis.get("queries") or [])
            n_cited = len(seed_analysis.get("cited_arxiv_ids") or [])
            ctx.progress("seed", f"种子分析完成：{n_queries} 条查询 / {n_cited} 条引文 / {len(seed_analysis.get('dataset_names') or [])} 个数据集")
            if seed_analysis.get("cited_arxiv_ids"):
                ctx.progress("seed", f"引文直取中（arXiv API，{n_cited} 条）")
                seed_citations = fetch_citation_papers(
                    seed_analysis["cited_arxiv_ids"], deepxiv=deepxiv_client_obj, log=_log
                )
                ctx.progress("seed", f"引文直取完成：{len(seed_citations)} 篇入候选池")
        ctx.check_cancel()

    # ---- 召回：种子派生查询（或 LLM 规划的英文查询组，或主题单查询）驱动多路融合 ----
    recall_queries: List[str] = []
    if seed_analysis and seed_analysis.get("queries"):
        recall_queries = list(seed_analysis["queries"])
    else:
        # 无种子：中文/小语种主题必须先转写成英文查询，否则英文文献库三路全空或漂移
        recall_queries = plan_recall_queries(ctx, factory)
    ctx.progress("recall", f"综述启动：{query}")
    papers = recall_papers(
        ctx,
        fetch_days=fetch_days,
        queries=recall_queries or None,
        seed_citations=seed_citations,
        use_deepxiv=use_deepxiv,
        use_kaggle=use_kaggle,
        coarse_top_k=coarse_top_k,
        embedding_endpoint=embedding_endpoint,
        embedding_api_key=embedding_api_key,
    )
    ctx.check_cancel()

    # ---- 召回-主题贴合度熔断：候选池与主题词面错位时直接终止 ----
    # 防幻觉综述：错位池子（如中文主题召回全库无关论文）若放行，写作阶段会
    # 凭领域通识硬写并产出与参考文献完全脱节的正文——宁可不生成。
    coverage_queries = recall_queries or [_intent_query_text(query)]
    coverage = recall_pool_lexical_coverage(papers, coverage_queries)
    ctx.funnel["lexical_coverage"] = round(coverage, 3)
    try:
        min_cov = float(os.getenv("DPR_SURVEY_RECALL_COVERAGE_MIN") or 0) or 0.2
    except ValueError:
        min_cov = 0.2
    if coverage < min_cov:
        raise RuntimeError(
            f"召回池与主题词面覆盖率过低（{coverage:.2f} < {min_cov}）：候选文献与主题明显错位，"
            "已终止以免生成幻觉综述。建议：改用英文主题、提供种子论文（锚定任务范式并派生检索词）、"
            "或调宽回溯范围；也可用 DPR_SURVEY_RECALL_COVERAGE_MIN 调整阈值。"
        )

    # ---- 语义粗排：万级候选 → embed_pool（本地 bge 直连），再进 rerank ----
    embed_pool = max(int(os.getenv("DPR_SURVEY_EMBED_POOL") or 0) or DEFAULT_EMBED_POOL, 1)
    papers = coarse_rank_papers(ctx, papers, recall_queries, embed_pool=embed_pool)
    ctx.funnel["embed_pool"] = len(papers)
    ctx.candidate_profile = summarize_candidate_profile(papers)
    ctx.check_cancel()

    ctx.funnel["rerank_in"] = len(papers)
    if use_rerank:
        papers = rerank_papers(ctx, papers, max_papers=max_papers)
    else:
        papers = papers[:max_papers]
    ctx.check_cancel()

    # 任务范式锚点：有种子直接用种子范式；否则主题归纳
    target_paradigm = define_task_paradigm(ctx, factory, seed_analysis=seed_analysis)
    extractions = extract_papers(
        ctx,
        papers,
        client_factory=factory,
        survey_topic=query,
        target_paradigm=target_paradigm,
    )
    ctx.check_cancel()

    # 终稿聚合：lane 对比统计（A/B 核心）+ 漏斗收口 + 种子引文覆盖率
    ctx.funnel["final"] = len(extractions)
    lane_stats_final = _aggregate_lane_stats(extractions, ctx.lane_stats)
    quality_warnings: List[str] = []
    relevance_values = []
    for record in extractions:
        try:
            relevance_values.append(float(record.get("relevance") or 0.0))
        except (TypeError, ValueError):
            pass
    if relevance_values:
        avg_relevance = round(sum(relevance_values) / len(relevance_values), 2)
        if avg_relevance < 5.5:
            note = f"终稿平均相关度偏低（avg_relevance={avg_relevance}），候选文献与主题的贴合情况建议人工复核"
            quality_warnings.append(note)
            ctx.warn(note)
    else:
        avg_relevance = None
    seed_ids = {_normalize_arxiv_id(str(p.get("paper_id") or "")) for p in seed_citations if p.get("paper_id")}
    if seed_ids:
        final_ids = {_normalize_arxiv_id(str(r.get("paper_id") or "")) for r in extractions}
        seed_citation_coverage = {
            "seed_refs": len(seed_ids),
            "in_final": len(seed_ids & final_ids),
            "rate": round(len(seed_ids & final_ids) / len(seed_ids), 3),
        }
    else:
        seed_citation_coverage = None
    lane_summary = "；".join(
        f"{src}: {stat.get('hits', 0)} 篇召回/{stat.get('papers_in_final', 0)} 篇入稿"
        f"（{stat.get('latency_s', 0)}s）"
        for src, stat in lane_stats_final.items()
    )
    if lane_summary:
        ctx.progress("analyse", f"召回路统计：{lane_summary}")

    clusters = cluster_papers(ctx, extractions, client_factory=factory)
    ctx.check_cancel()

    deep_read_core_papers(ctx, clusters, extractions, enabled=deep_read, deepxiv=deepxiv_client_obj)
    ctx.check_cancel()

    cluster_analyses, global_analysis = analyse_clusters(ctx, clusters, extractions, client_factory=factory)
    ctx.check_cancel()

    outline = build_outline(ctx, global_analysis=global_analysis, clusters=clusters, client_factory=factory)
    ctx.check_cancel()

    section_markdowns, sections = write_sections(
        ctx,
        outline=outline,
        clusters=clusters,
        cluster_analyses=cluster_analyses,
        global_analysis=global_analysis,
        extractions=extractions,
        client_factory=factory,
    )
    ctx.check_cancel()

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    draft = assemble_report(
        query=query,
        title_zh=outline["title_zh"],
        section_markdowns=section_markdowns,
        extractions=extractions,
        clusters=clusters,
        generated_at=generated_at,
        non_arxiv_refs=ctx.non_arxiv_refs,
    )
    final_md, issues = review_draft(ctx, draft, client_factory=factory)

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    _log(f"综述流水线完成：{len(extractions)} 篇论文 / {len(clusters)} 簇 / {len(sections)} 节 / 耗时 {elapsed:.0f}s")
    return {
        "query": query,
        "seed": {
            "arxiv_id": (seed_paper or {}).get("arxiv_id") if seed_paper else None,
            "analyzed": bool(seed_analysis),
            "n_citations_fetched": len(seed_citations),
        } if seed_paper else None,
        "papers": extractions,
        "clusters": [
            {
                "cluster_id": c["cluster_id"],
                "name_zh": c["name_zh"],
                "keywords": c.get("keywords") or [],
                "paper_ids": [extractions[i]["paper_id"] for i in c["paper_indices"]],
            }
            for c in clusters
        ],
        "cluster_analyses": cluster_analyses,
        "global_analysis": global_analysis,
        "outline": {"title_zh": outline["title_zh"], "sections": [s["heading"] for s in sections]},
        "report_markdown": final_md,
        "review_issues": issues,
        "report_meta": {
            "generated_at": generated_at,
            "elapsed_seconds": round(elapsed, 1),
            "n_papers": len(extractions),
            "n_clusters": len(clusters),
            "n_sections": len(sections),
            "deep_read": bool(deep_read),
            "used_rerank": bool(use_rerank),
            "seeded": bool(seed_analysis),
            "used_deepxiv": bool(use_deepxiv and deepxiv_client_obj),
            "used_kaggle": bool(use_kaggle and ctx.lane_stats.get("kaggle")),
            "lane_stats": lane_stats_final,
            "funnel": dict(ctx.funnel),
            "recall_coherence": ctx.recall_coherence,
            "avg_relevance": avg_relevance,
            "quality_warnings": quality_warnings,
            "seed_citation_coverage": seed_citation_coverage,
            "candidate_profile": ctx.candidate_profile,
        },
        "warnings": ctx.warnings,
    }


if __name__ == "__main__":
    import argparse

    try:
        from local_env import load_local_env  # noqa: E402
    except ImportError:
        from src.local_env import load_local_env  # noqa: E402
    load_local_env()

    parser = argparse.ArgumentParser(description="原生综述流水线（本地 CLI 调试入口）")
    parser.add_argument("--query", required=True)
    parser.add_argument("--seed", default="", help="种子论文 arXiv 链接/id（可选）")
    parser.add_argument("--max-papers", type=int, default=DEFAULT_MAX_PAPERS)
    parser.add_argument("--fetch-days", type=int, default=DEFAULT_FETCH_DAYS)
    parser.add_argument("--no-rerank", action="store_true")
    parser.add_argument("--no-deep-read", action="store_true")
    parser.add_argument("--deepxiv", action="store_true", help="启用 DeepXiv 外部检索（默认关：走 Kaggle 快照粗筛）")
    parser.add_argument("--no-kaggle", action="store_true", help="关闭 Kaggle 快照粗筛路")
    parser.add_argument("--coarse-top-k", type=int, default=None, help="Kaggle 词法粗筛候选量（默认 10000）")
    args = parser.parse_args()
    seed: Optional[Dict[str, Any]] = None
    if args.seed:
        from survey_seed import extract_arxiv_id  # noqa: E402

        seed_id = extract_arxiv_id(args.seed)
        if not seed_id:
            raise SystemExit("--seed 需要是 arXiv 链接或 id")
        seed = {"arxiv_id": seed_id}
    result = run_survey(
        args.query,
        max_papers=args.max_papers,
        fetch_days=args.fetch_days,
        use_rerank=not args.no_rerank,
        deep_read=not args.no_deep_read,
        seed_paper=seed,
        use_deepxiv=args.deepxiv,
        use_kaggle=not args.no_kaggle,
        coarse_top_k=args.coarse_top_k,
        on_progress=lambda stage, message, current=None, total=None: _log(f"[{stage}] {message}"),
    )
    print(json.dumps(result["report_meta"], ensure_ascii=False, indent=2))
    out = ROOT_DIR / ".local-runs" / f"survey-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(result["report_markdown"], encoding="utf-8")
    print(f"报告已写入：{out}")
