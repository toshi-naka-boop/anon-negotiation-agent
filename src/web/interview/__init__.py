"""面談(web, design.md §5)の API とロジック。画面は含まない。

- 決定的なロジック(LLM なし): プロフィール → 属性帯(profile)、年収の換算(salary)、パッケージ二択の生成(choices)、
  二択の回答・発言 → アンカー(statements)、平文の文(sentences)、アンカーの一覧・矛盾・「最悪ここまで」・送信の形(anchors)、
  企業の一覧(companies)。文面は fixtures/interview_templates.toml(templates。暫定・U-01)。
- 面談エージェント(agent): ADK の LlmAgent 2 体(年収の読み取り・発言の構造化)。JSON モード、呼び出しごとに新しいセッション、
  送る前に物理の呼び出し数を計上する。
- 途中の状態(state): サーバのメモリに依頼者 ID ごとに持つ。進行(service)と API(api。web.api が include する別の APIRouter)。
- 設定: config/params.toml の [web.interview](config)。

web.services が build_interview_service で組み立てる。web.interview.api は、web.services を import しない(循環を避けるため、
このパッケージの __init__ は api を import しない)。
"""

from google.adk.models.base_llm import BaseLlm

from vault.clock import Clock

from web.config import WebConfig
from web.interview.agent import InterviewAgent, Sleep
from web.interview.companies import list_companies
from web.interview.config import DEFAULT_INTERVIEW_CONFIG, InterviewConfig
from web.interview.salary import SalaryBasis
from web.interview.service import InterviewError, InterviewService
from web.interview.state import InterviewStateStore
from web.interview.statements import ConstraintList
from web.interview.templates import InterviewTemplates, default_templates
from web.llm_budget import LlmBudget
from web.principals_meta import PrincipalsMetaStore
from web.vault_client import VaultClient

# SalaryBasis・ConstraintList は面談エージェントの出力の型(§5 の 2・4・5)。negotiation_core の schema にはないので、ここに置いた。
__all__ = ["ConstraintList", "InterviewError", "InterviewService", "SalaryBasis", "build_interview_service"]

_SECONDS_PER_DAY = 86400


def build_interview_service(
    *,
    vault: VaultClient,
    meta: PrincipalsMetaStore,
    llm_budget: LlmBudget,
    clock: Clock,
    sleep: Sleep,
    web_config: WebConfig,
    config: InterviewConfig = DEFAULT_INTERVIEW_CONFIG,
    templates: InterviewTemplates | None = None,
    companies: list[dict[str, str]] | None = None,
    model: BaseLlm | None = None,
) -> InterviewService:
    """面談の部品を組み立てる。設問のテンプレートが設計と食い違っていれば、ここで ValueError(起動を止める)。

    model を省くと、設定のモデル名の ADK `Gemini`(クライアント側の自動再試行を切ったもの)を、最初の LLM 呼び出しのときに作る
    (組み立てでは接続しない。テストは、use_model でスタブを差し込む)。LLM の再試行の規則は、レフェリーと同じ(web_config.referee)。
    """
    agent = InterviewAgent(
        budget=llm_budget, clock=clock, sleep=sleep, retry=web_config.referee, config=config, model=model
    )
    store = InterviewStateStore(
        clock,
        idle_ttl_seconds=config.state_idle_ttl_seconds,
        max_lifetime_seconds=config.max_lifetime_seconds,
        max_states=config.max_active_interviews,
        max_per_client=config.max_concurrent_per_client,
    )
    return InterviewService(
        vault=vault,
        meta=meta,
        agent=agent,
        store=store,
        templates=templates if templates is not None else default_templates(),
        config=config,
        limits=web_config.limits,
        retention_days=web_config.principals.retention_seconds // _SECONDS_PER_DAY,
        companies=companies if companies is not None else list_companies(),
    )
