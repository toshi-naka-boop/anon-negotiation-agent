"""ケース 1 のフィクスチャ(design.md §8.4)と、台本のエージェントでの到達の確認(DV-14。§12.2)。

フィクスチャの性質(本物の LLM・GCP は使わない。純粋な計算と、Firestore エミュレータの金庫だけ)
- リモート 0 日・当直なしでは、年収だけで合意できる組み合わせがない(「年収だけでは合意に届かない」。AC-09)。
- 両者が受けられる組み合わせが、7 巡目のシミュレーションと同じ 288 通り以上ある。
- 丸め済みポリシーに矛盾がなく、判定が決まらない組み合わせもない。生の条件と、丸め済みポリシーの判定が一致する。
- 途中確認への自動回答(FixtureAnswerer)が、生の条件と一致する。レフェリー経由で、金庫に追記される。
- フィクスチャは、金庫のテンプレート(§3.7)に置け、交渉の作成時に、そこから写される。
- 読み込みは、形の違い・グリッド外の値・同じ組の重複・矛盾する条件を拒否する。

DV-14(名前に case1_reachability を含むテスト)
- web のレフェリーと、本物の金庫(Firestore エミュレータ)を通して、ケース 1 を交渉させる。交渉する側は、LLM を使わない
  台本のエージェント(tests/scripted_negotiators.py。Gemini と同じく TurnInput だけを見る)。v14 の計画・決定の形で動かし、
  レフェリーの確かめの実行条件(残りの評価回数 > 残りの手数 ＋ 残りの途中確認数。台帳 C-47・X-59)を通す。
- 36 通りの始め方(§8.4。候補者の最初の手 6 通り × 求人の最初の手 6 通り)× 探し方(7 巡目の表の行)。
  上限は config/params.toml の値のまま(側ごとに手数 6・評価 17・途中確認 1。v14)。
- 合否に入れる探し方(1・2・5 行目)は、それぞれ 36 通り中 34 通り以上が合意(judged・agreed)に届くこと。
  記録だけの探し方(3・4・6 行目と、5 行目の向きを入れ替えたもの)の結果は、表にして出力する(合否に入れない)。

ケース 2(AC-10。名前に case2 を含むテスト)
- 全 18,000 通りの総当たりで、両者が受けられる組み合わせが 0 件(丸め済みポリシーでも、生の条件でも)。候補者が受ける最低の年収が、
  求人が受ける最高の年収を上回るので、年収以外の軸をどう動かしても合意できない。
- 台本のエージェント(7 つの探し方 × 4 通りの始め方)で始めても、合意に至らず、手数か評価の上限で止まって(judged)、結果は双方に「なし」。

ケース 3(AC-09〜11・AC-12。§8.2・§8.3・§8.4。名前に case3 を含むテスト)
- 候補者は demo-candidate-1(受けられる集合はケース 1 の候補者と同程度の広さ。生の境目はグリッド上にない値)。求人は何でも受ける。
- 探索線(年収以外の軸を固定した線。tests/scripted_negotiators.py の SEARCH_LINE)の上で、受ける境目と受けない境目が隣り合うマスにある
  (「本人確認が必要」の隙間がない)。台本の攻撃者が年収を二分探索しても、評価の上限(17 回)の中で、区間(estimate_interval)は
  グリッド 1 マス(600 万より上、650 万以下)で止まり、候補者の生の境目(620 万)までは分からない。グリッドの全点を問い合わせても同じ。
- estimate_interval の単体の確かめ(§8.3 の計算)。
"""

import itertools
import math
from collections import Counter
from dataclasses import dataclass

import pytest

from negotiation_core import (
    AXES,
    Package,
    Verdict,
    contained_in,
    evaluate,
    iter_all_packages,
    satisfies,
)
from negotiation_core.estimate_interval import estimate_interval

from scripted_negotiators import (
    SALARY_GRID,
    SEARCH_LINE,
    Negotiator,
    ScriptedAttackNegotiators,
    ScriptedAttacker,
    ScriptedNegotiators,
    Strategy,
    is_on_search_line,
    next_probe_salary,
    probe_package,
)
from vault.config import DEFAULT_VAULT_CONFIG
from vault.fixtures import (
    FIXTURES_DIRECTORY,
    CaseFixture,
    RawConditions,
    build_rounded_policy,
    load_case_fixture,
    put_fixture_templates,
)
from vault.models import EmployerRule
from vault.templates import get_template, put_template
from vault_helpers import (
    accept_all_policy,
    demo_create_request,
    make_candidate_template,
    make_employer_template,
    needs_confirmation_policy,
)
from web.fictional_answerer import FixtureAnswerer
from web.referee import NegotiationContext, Referee, RefereeDeps
from web_helpers import drive, move_dict

# 7 巡目の人物(design/anon-negotiation-agent/reviews/round-7-sim/exp6.py の cmin・emax)の判定を、文章どおりに書いたもの。
# フィクスチャの表とは別に持ち、フィクスチャが同じ人物になっていることの基準にする。


def candidate_accepts(package: Package) -> bool:
    """候補者: リモート 0 日なら 700 万以上、1 日なら 650 万以上、2 日以上なら 600 万以上。当直は月 2 回まで。見直しは気にしない。"""
    if package.night_duty > 2:
        return False
    return package.salary >= {0: 700, 1: 650}.get(package.remote_days, 600)


def employer_accepts(package: Package) -> bool:
    """求人: リモート 2 日までなら上限 650 万、3 日なら 600 万、4 日以上は不可。当直が月 2 回以上なら上限を 50 万上げる。"""
    if package.remote_days >= 4:
        return False
    cap = 650 if package.remote_days <= 2 else 600
    return package.salary <= cap + (50 if package.night_duty >= 2 else 0)


ALL_PACKAGES = tuple(iter_all_packages())
ROUND_7_MUTUAL_PACKAGES = 288  # 7 巡目のシミュレーションの値(両者が受けられる組み合わせの数)


@pytest.fixture(scope="module")
def case1() -> CaseFixture:
    return load_case_fixture(1)


# ----------------------------------------------------------------------
# フィクスチャの性質
# ----------------------------------------------------------------------


def test_case1_fixture_has_every_part_of_the_format(case1):
    # §8.4: 候補者(生の条件・丸め済みポリシー・属性帯・職務要約・連絡先)と、求人(企業名・公開求人・生の裁量・
    # 丸め済みポリシー(属性帯の条件付き)・自動応答の設定)がそろっている。
    candidate, employer = case1.candidate, case1.employer
    assert candidate.raw.bounds
    assert candidate.policy.accept_anchors and candidate.policy.reject_anchors
    assert candidate.attribute_bands.job_category == "it_web"
    assert candidate.job_summary and candidate.contact.name and candidate.contact.email.endswith("@example.com")
    assert employer.company_name and employer.public_job.title
    assert employer.auto_response.meet is True and employer.auto_response.approve is True
    assert len(employer.rules) == 1
    rule = employer.rules[0]
    assert rule.when == {} and rule.raw.bounds
    assert rule.policy.accept_anchors and rule.policy.reject_anchors
    assert employer.rule_for(candidate.attribute_bands) is rule


def test_case1_raw_conditions_are_the_round_7_people(case1):
    # 7 巡目の手作りのケース 1 の人物(文章どおりの判定)と、表の生の条件が、全 18,000 通りで一致する。
    rule = case1.employer.rules[0]
    assert all(case1.candidate.raw.accepts(p) == candidate_accepts(p) for p in ALL_PACKAGES)
    assert all(rule.raw.accepts(p) == employer_accepts(p) for p in ALL_PACKAGES)


def test_case1_rounded_policies_agree_with_the_raw_conditions_and_leave_no_gap(case1):
    # 丸め済みポリシーは、生の条件と同じ判定を、全 18,000 通りで返す(§2.5)。矛盾(受けるアンカーと受けないアンカーの
    # 両方に当てはまる組み合わせ)も、判定が決まらない(本人確認が必要になる)組み合わせもない。
    for policy, raw in (
        (case1.candidate.policy, case1.candidate.raw),
        (case1.employer.rules[0].policy, case1.employer.rules[0].raw),
    ):
        for package in ALL_PACKAGES:
            accepted = any(satisfies(package, a, policy.side) for a in policy.accept_anchors)
            rejected = any(contained_in(package, a, policy.side) for a in policy.reject_anchors)
            assert accepted != rejected  # どちらか一方だけ
            assert accepted == raw.accepts(package)


def test_case1_has_no_agreement_on_salary_alone_when_fully_onsite_without_night_duty(case1):
    # 「年収だけでは合意に届かない」: リモート 0 日・当直なしでは、年収をどう動かしても、両者が受けられる組み合わせがない
    # (年収以外の軸を動かせば、合意できる組み合わせがある。下の広さのテスト)。
    candidate_policy, employer_policy = case1.candidate.policy, case1.employer.rules[0].policy
    onsite = [p for p in ALL_PACKAGES if p.remote_days == 0 and p.night_duty == 0]
    assert onsite
    for package in onsite:
        assert not (
            evaluate(candidate_policy, package) is Verdict.ACCEPTABLE
            and evaluate(employer_policy, package) is Verdict.ACCEPTABLE
        )
    # 候補者が受ける年収の下限と、求人が受ける年収の上限が、重ならないことを境目で示す。
    assert min(p.salary for p in onsite if evaluate(candidate_policy, p) is Verdict.ACCEPTABLE) == 700
    assert max(p.salary for p in onsite if evaluate(employer_policy, p) is Verdict.ACCEPTABLE) == 650


def test_case1_mutual_breadth_is_at_least_the_round_7_value(case1):
    # 両者が受けられる組み合わせの広さ(§3.6)が、7 巡目のシミュレーションの値(288 通り)以上ある。
    candidate_policy, employer_policy = case1.candidate.policy, case1.employer.rules[0].policy
    mutual = [
        p
        for p in ALL_PACKAGES
        if evaluate(candidate_policy, p) is Verdict.ACCEPTABLE and evaluate(employer_policy, p) is Verdict.ACCEPTABLE
    ]
    assert len(mutual) >= ROUND_7_MUTUAL_PACKAGES
    # リモートや当直を動かせば合意できる: 年収 700 万でリモート 0 日でも、当直が月 2 回なら、両者が受けられる。
    assert any(p.remote_days == 0 and p.night_duty == 2 and p.salary == 700 for p in mutual)


@pytest.mark.anyio
@pytest.mark.parametrize("side", ["candidate", "employer"])
async def test_case1_answerer_matches_the_raw_conditions(case1, side):
    # 自動回答(§4.4)が、生の条件(文章どおりの判定)と、全 18,000 通りで一致する。
    answerer = FixtureAnswerer(case1)
    accepts = candidate_accepts if side == "candidate" else employer_accepts
    for package in ALL_PACKAGES:
        expected = "accept" if accepts(package) else "reject"
        assert await answerer(nid="0123456789abcdef", side=side, package=package) == expected


# ----------------------------------------------------------------------
# 読み込みの検証
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        pytest.param(  # 同じ組(リモート 0 日・当直なし・見直し 6 か月)を、2 つの行に書く
            "[employer]\ntemplate_id",
            "[[candidate.raw_conditions]]\nremote_days = [0]\nnight_duty = [0]\nmin_salary = 800\n\n[employer]\ntemplate_id",
            "same combination",
            id="rows_overlap",
        ),
        pytest.param(  # 当直 3 回は、グリッドにない値
            "night_duty = [0, 2]\nmin_salary = 700",
            "night_duty = [0, 3]\nmin_salary = 700",
            "not on the grid",
            id="off_grid_value",
        ),
        pytest.param(  # リモートが多いほど、年収を高く求める(丸めた後のアンカーが矛盾する)
            "remote_days = [2, 3, 4, 5]\nnight_duty = [0, 2]\nmin_salary = 600",
            "remote_days = [2, 3, 4]\nnight_duty = [0, 2]\nmin_salary = 600\n\n"
            "[[candidate.raw_conditions]]\nremote_days = [5]\nnight_duty = [0, 2]\nmin_salary = 900",
            "contradict",
            id="contradictory_conditions",
        ),
        pytest.param(  # 候補者の行に、求人の項目(max_salary)を書く
            "min_salary = 700",
            "min_salary = 700\nmax_salary = 800",
            "Extra inputs are not permitted",
            id="unknown_key",
        ),
    ],
)
def test_case1_loader_rejects_an_invalid_file(tmp_path, old, new, message):
    text = (FIXTURES_DIRECTORY / "case1.toml").read_text(encoding="utf-8")
    assert old in text
    (tmp_path / "case1.toml").write_text(text.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_case_fixture(1, directory=tmp_path)


# ----------------------------------------------------------------------
# 金庫のテンプレートへの登録と、交渉の作成
# ----------------------------------------------------------------------


def test_case1_templates_are_put_into_the_vault_and_copied_into_a_negotiation(store, case1):
    # 金庫のテンプレート(§3.7)に置け、そのまま読み戻せる。置き直しても同じ(同じ template_id は置き換わる)。
    put_fixture_templates(store._db, case1)
    put_fixture_templates(store._db, case1)
    candidate_template, employer_template = case1.templates()
    assert get_template(store._db, case1.candidate.template_id) == candidate_template
    assert get_template(store._db, case1.employer.template_id) == employer_template

    # 交渉(デモ)の作成時に、テンプレートから交渉用コピーが写される(求人側は、候補者の属性帯に当てはまる規則)。
    created = store.create_negotiation(demo_create_request(case1.candidate.template_id, case1.employer.template_id))
    assert created.status == "created"
    document = store._negotiation_ref(created.nid).get().to_dict()
    assert document["participants"]["employer"]["job_id"] == case1.employer.job_id
    assert document["participants"]["employer"]["company_id"] == case1.employer.company_id
    assert document["participants"]["candidate"]["attribute_bands"] == case1.candidate.attribute_bands.model_dump()
    copy_candidate = document["snapshots"]["candidate"]
    copy_employer = document["snapshots"]["employer"]
    assert len(copy_candidate["accept_anchors"]) == len(case1.candidate.policy.accept_anchors)
    assert len(copy_candidate["reject_anchors"]) == len(case1.candidate.policy.reject_anchors)
    assert len(copy_employer["accept_anchors"]) == len(case1.employer.rules[0].policy.accept_anchors)
    assert len(copy_employer["reject_anchors"]) == len(case1.employer.rules[0].policy.reject_anchors)


# ----------------------------------------------------------------------
# 自動回答が、レフェリー経由で金庫に追記される
# ----------------------------------------------------------------------


def _opening(**axes) -> Package:
    """最初の手(最初の提案にする組み合わせ)。指定しない軸は、7 巡目のシミュレーションと同じ値(区分軸は「あり・可・3 か月以内」)。"""
    return Package(training="available", side_job="allowed", start="within_3_months", **axes)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("asking_side", "salary", "expected_answer"),
    [
        ("candidate", 700, "accept"),  # 候補者: リモート 0 日なら 700 万以上で受ける
        ("candidate", 650, "reject"),
        ("employer", 650, "accept"),  # 求人: リモート 0 日・当直なしなら、上限 650 万
        ("employer", 700, "reject"),
    ],
)
async def test_case1_answerer_answers_the_referee_and_the_vault_appends_the_answer(
    store, web_env, case1, asking_side, salary, expected_answer
):
    # 途中確認(ask_principal)に、レフェリーが自動回答(FixtureAnswerer)を呼び、金庫が回答を交渉用コピーに追記する(§4.4)。
    # 追記の後は、同じ組み合わせの評価が、生の条件の答えどおりに決まる(回答の記録は、金庫が追記と同じトランザクションで評価し直した
    # 結果を持つ)。ケース 1 の丸め済みポリシーは、判定が決まらない組み合わせを持たないので、聞かれる側のポリシーだけを
    # 「何も決まっていない」ものに替えて確かめる。
    package = _opening(salary=salary, remote_days=0, night_duty=0, review_months=6)
    candidate_policy = (
        needs_confirmation_policy("candidate") if asking_side == "candidate" else accept_all_policy("candidate")
    )
    employer_policy = (
        needs_confirmation_policy("employer") if asking_side == "employer" else accept_all_policy("employer")
    )
    candidate_template = make_candidate_template(policy=candidate_policy, attribute_bands=case1.candidate.attribute_bands)
    employer_template = make_employer_template(rules=[EmployerRule(when={}, policy=employer_policy)])
    put_template(store._db, candidate_template)
    put_template(store._db, employer_template)
    created = store.create_negotiation(
        demo_create_request(candidate_template.template_id, employer_template.template_id)
    )
    assert created.status == "created"

    env = web_env
    env.configure(answerer=FixtureAnswerer(case1))
    ask_then_end = (move_dict("ask_principal", package), move_dict("end"))
    if asking_side == "candidate":
        env.agents.script("candidate", *ask_then_end)
    else:
        env.agents.script("candidate", move_dict("propose", package))
        env.agents.script("employer", *ask_then_end)
    await drive(env.referee(created.nid))

    events = store.get_events(created.nid, asking_side)
    answer = next(e for e in events if e.kind == "principal_answer")
    assert answer.answer == expected_answer
    expected_verdict = Verdict.ACCEPTABLE if expected_answer == "accept" else Verdict.NOT_ACCEPTABLE
    assert Verdict(answer.own_evaluation) is expected_verdict


# ----------------------------------------------------------------------
# DV-14: 台本のエージェントでの到達の確認
# ----------------------------------------------------------------------

SALARIES_OF_THE_CANDIDATE_OPENING = (800, 900, 1000)
REMOTE_DAYS_OF_THE_CANDIDATE_OPENING = (3, 5)
SALARIES_OF_THE_EMPLOYER_OPENING = (400, 500)
NIGHT_DUTIES_OF_THE_EMPLOYER_OPENING = (2, 4, 8)
REQUIRED_AGREEMENTS = 34  # 合否に入れる探し方が、36 通り中これ以上で合格(§8.4)


def the_36_starts() -> list[tuple[Package, Package]]:
    """36 通りの始め方(§8.4): 候補者の最初の手 6 通り × 求人の最初の手 6 通り。"""
    candidate_openings = [
        _opening(salary=salary, remote_days=remote_days, night_duty=0, review_months=6)
        for salary, remote_days in itertools.product(
            SALARIES_OF_THE_CANDIDATE_OPENING, REMOTE_DAYS_OF_THE_CANDIDATE_OPENING
        )
    ]
    employer_openings = [
        _opening(salary=salary, remote_days=0, night_duty=night_duty, review_months=12)
        for salary, night_duty in itertools.product(
            SALARIES_OF_THE_EMPLOYER_OPENING, NIGHT_DUTIES_OF_THE_EMPLOYER_OPENING
        )
    ]
    return list(itertools.product(candidate_openings, employer_openings))


@dataclass(frozen=True)
class SearchType:
    """探し方の型(7 巡目の表の行)。両側の探し方と、合否に入れるか、7 巡目の合意の数(36 通り中。表にない型は None)。"""

    row: str
    label: str
    candidate: Strategy
    employer: Strategy
    gates: bool
    round_7_agreed: int | None


_TRADE = Strategy("trade")
_HYBRID = Strategy("hybrid")
_CONCEDE_AFTER_THREE = Strategy("concede", switch=3)
SEARCH_TYPES = (
    SearchType("1", "年収は差の半分、他の軸は 1 段ずつ一緒に譲る", _TRADE, _TRADE, True, 36),
    SearchType("2", "1 に加え、差が縮んだら相手の案を 1 段寄せて確かめる", _HYBRID, _HYBRID, True, 36),
    SearchType(
        "3",
        "年収を 1 段ずつ譲り、2 回目の提案から他の軸も寄せる",
        Strategy("concede", switch=1),
        Strategy("concede", switch=1),
        False,
        34,
    ),
    SearchType(
        "4", "3 回目の提案まで年収だけを譲り、4 回目から他の軸も寄せる", _CONCEDE_AFTER_THREE, _CONCEDE_AFTER_THREE, False, 24
    ),
    SearchType("5", "片側が 2 行目(候補者)、もう片側が 4 行目(求人)", _HYBRID, _CONCEDE_AFTER_THREE, True, 36),
    SearchType("6", "1 行目の型で、確認せずに譲歩案を出す", Strategy("trade", check_first=False), Strategy("trade", check_first=False), False, 22),
    # 7 巡目の表にない: 5 行目の向きを入れ替えたもの(§8.4 は、どちらの側が外れるかを決めていない)
    SearchType("5(逆)", "片側が 4 行目(候補者)、もう片側が 2 行目(求人)", _CONCEDE_AFTER_THREE, _HYBRID, False, None),
)


async def negotiate_to_the_end(env, store, fixture, candidate: Negotiator, employer: Negotiator) -> dict:
    """フィクスチャのテンプレートから、デモの交渉を 1 つ作り、レフェリーで終わりまで進めて、金庫の交渉の文書を返す。

    終わりまで進まなければ(レフェリーが待ち続ける・手が止まる)、失敗にする。結果の読み替えはしない。
    """
    created = store.create_negotiation(demo_create_request(fixture.candidate.template_id, fixture.employer.template_id))
    assert created.status == "created"
    deps = RefereeDeps(
        vault=env.vault,
        send_turn=ScriptedNegotiators(candidate, employer),
        clock=env.clock,
        sleep=env.sleep,
        config=env.config,
        answerer=FixtureAnswerer(fixture),
        count_llm_calls=False,  # 台帳 X-60: 計上はこのテストの対象外
    )
    referee = Referee(NegotiationContext(nid=created.nid, mode="demo", candidate_principal_id=None), deps)
    await drive(referee, max_steps=100)
    return store._negotiation_ref(created.nid).get().to_dict()


def format_reachability_report(results: dict[str, Counter]) -> str:
    """探し方ごとの結果(合意の数と、合意以外の終わり方)を、7 巡目の値と並べた表にする。"""
    total = len(the_36_starts())
    limits = DEFAULT_VAULT_CONFIG.limits
    lines = [
        f"DV-14 ケース 1 の到達(台本のエージェント。{total} 通りの始め方。上限は側ごとに手数 {limits.moves_budget_per_side}・"
        f"評価 {limits.evaluation_budget_per_side}・途中確認 {limits.principal_checks_per_side})",
        "行 | 扱い | 合意 | 7 巡目 | 合意以外の終わり方 | 探し方",
    ]
    for search_type in SEARCH_TYPES:
        counts = results[search_type.row]
        others = ", ".join(f"{reason} {n}" for reason, n in sorted(counts.items()) if reason != "agreed") or "なし"
        round_7 = "-" if search_type.round_7_agreed is None else f"{search_type.round_7_agreed}/{total}"
        lines.append(
            f"{search_type.row} | {'合否に入れる' if search_type.gates else '記録だけ'} | {counts['agreed']}/{total}"
            f" | {round_7} | {others} | {search_type.label}"
        )
    return "\n".join(lines)


@pytest.mark.anyio
async def test_case1_reachability_of_scripted_negotiators_within_the_limits(store, web_env, case1, capsys):
    # DV-14: 36 通りの始め方 × 探し方を、本物の金庫(エミュレータ)とレフェリーを通して交渉させる。
    # 合否に入れる 3 つの探し方(1・2・5 行目)は、それぞれ 36 通り中 34 通り以上が、側ごとの上限の中で合意(judged・agreed)に届く。
    # 記録だけの探し方の結果は、表に出す(-s なしでも見える)。
    put_fixture_templates(store._db, case1)
    starts = the_36_starts()
    assert len(starts) == 36

    results: dict[str, Counter] = {}
    for search_type in SEARCH_TYPES:
        counts: Counter = Counter()
        for candidate_opening, employer_opening in starts:
            document = await negotiate_to_the_end(
                web_env,
                store,
                case1,
                Negotiator(search_type.candidate, candidate_opening),
                Negotiator(search_type.employer, employer_opening),
            )
            assert document["status"] == "judged"
            counts[document["end_reason"]] += 1
            if document["end_reason"] == "agreed":
                # 合意した組み合わせは、「中」以上の見込みで、7 巡目の人物(文章どおりの判定)の両者が受けられる。
                result = document["result"]
                assert result["likelihood"] in ("high", "medium")
                agreed = Package(**result["package"])
                assert candidate_accepts(agreed) and employer_accepts(agreed)
        results[search_type.row] = counts

    report = format_reachability_report(results)
    with capsys.disabled():
        print("\n" + report)
    for search_type in SEARCH_TYPES:
        if search_type.gates:
            assert results[search_type.row]["agreed"] >= REQUIRED_AGREEMENTS, report


# ----------------------------------------------------------------------
# ケース 2: 両者が受けられる組み合わせがない(AC-10)
# ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def case2() -> CaseFixture:
    return load_case_fixture(2)


def test_case2_fixture_has_every_part_of_the_format(case2):
    # §8.4: ケース 1 と同じ形(候補者: 生の条件・丸め済みポリシー・属性帯・職務要約・連絡先 / 求人: 企業名・公開求人・生の裁量・
    # 丸め済みポリシー・自動応答の設定)。
    candidate, employer = case2.candidate, case2.employer
    assert candidate.raw.bounds and candidate.policy.accept_anchors and candidate.policy.reject_anchors
    assert candidate.attribute_bands.job_category == "it_web"
    assert candidate.job_summary and candidate.contact.name and candidate.contact.email.endswith("@example.com")
    assert employer.company_name and employer.public_job.title
    assert employer.auto_response.meet is True and employer.auto_response.approve is True
    (rule,) = employer.rules
    assert rule.when == {} and rule.raw.bounds and rule.policy.accept_anchors and rule.policy.reject_anchors
    assert employer.rule_for(candidate.attribute_bands) is rule


def test_case2_has_no_combination_that_both_sides_accept(case2):
    # AC-10: 全 18,000 通りの総当たりで、両者が受けられる組み合わせが 0 件(丸め済みポリシーでも、生の条件でも)。
    candidate_policy, employer_rule = case2.candidate.policy, case2.employer.rules[0]
    assert not [
        p
        for p in ALL_PACKAGES
        if evaluate(candidate_policy, p) is Verdict.ACCEPTABLE and evaluate(employer_rule.policy, p) is Verdict.ACCEPTABLE
    ]
    assert not [p for p in ALL_PACKAGES if case2.candidate.raw.accepts(p) and employer_rule.raw.accepts(p)]


def test_case2_candidate_asks_more_than_the_employer_pays_whatever_the_other_axes_are(case2):
    # 候補者が受ける最低の年収(750 万)が、求人が受ける最高の年収(700 万)を上回る。年収以外の軸(リモート・当直・昇給見直し)を
    # どう動かしても、この差は埋まらない。いちばん近い組み合わせでも、年収で 1 マス(50 万)足りない。
    lowest = min(p.salary for p in ALL_PACKAGES if evaluate(case2.candidate.policy, p) is Verdict.ACCEPTABLE)
    highest = max(p.salary for p in ALL_PACKAGES if evaluate(case2.employer.rules[0].policy, p) is Verdict.ACCEPTABLE)
    assert (lowest, highest) == (750, 700)
    assert lowest - highest == SALARY_GRID[1] - SALARY_GRID[0]


def test_case2_both_sides_are_real_people_with_no_gap(case2):
    # どちらも、何も受けない・何でも受ける、という中身のない人物ではない。判定が決まらない組み合わせもない(途中確認は起きない)。
    for policy in (case2.candidate.policy, case2.employer.rules[0].policy):
        verdicts = Counter(evaluate(policy, p) for p in ALL_PACKAGES)
        assert verdicts[Verdict.NEEDS_CONFIRMATION] == 0
        assert verdicts[Verdict.ACCEPTABLE] >= 1000 and verdicts[Verdict.NOT_ACCEPTABLE] >= 1000


@pytest.mark.parametrize("case", [2, 3])
def test_case2_and_case3_rounded_policies_agree_with_the_raw_conditions(case):
    # 丸め済みポリシー(金庫が見るもの)は、生の条件と同じ判定を、全 18,000 通りで返す(§2.5)。ケース 3 の生の境目は、グリッド上にない値
    # (680・620・570 万)を含むが、グリッド上の組み合わせの判定は変わらない。矛盾も、判定が決まらない組み合わせもない。
    fixture = load_case_fixture(case)
    rule = fixture.employer.rules[0]
    for policy, raw in ((fixture.candidate.policy, fixture.candidate.raw), (rule.policy, rule.raw)):
        for package in ALL_PACKAGES:
            accepted = any(satisfies(package, a, policy.side) for a in policy.accept_anchors)
            rejected = any(contained_in(package, a, policy.side) for a in policy.reject_anchors)
            assert accepted != rejected
            assert accepted == raw.accepts(package)


@pytest.mark.anyio
async def test_case2_scripted_negotiators_never_agree_and_stop_within_the_limits(store, web_env, case2, capsys):
    # AC-10: 台本のエージェント(7 つの探し方。確かめずに出す型を含む)が、4 通りの始め方で交渉しても、合意に至らない。手数か評価の
    # 上限で止まり(judged)、結果は双方に「なし」だけ。上限(側ごとに手数 6・評価 17・途中確認 1)の中で終わる。
    put_fixture_templates(store._db, case2)
    limits = DEFAULT_VAULT_CONFIG.limits
    starts = [
        (
            _opening(salary=candidate_salary, remote_days=3, night_duty=0, review_months=6),
            _opening(salary=employer_salary, remote_days=0, night_duty=4, review_months=12),
        )
        for candidate_salary, employer_salary in itertools.product((900, 1000), (400, 500))
    ]
    ends: Counter = Counter()
    for search_type in SEARCH_TYPES:
        for candidate_opening, employer_opening in starts:
            document = await negotiate_to_the_end(
                web_env,
                store,
                case2,
                Negotiator(search_type.candidate, candidate_opening),
                Negotiator(search_type.employer, employer_opening),
            )
            assert document["status"] == "judged"
            assert document["end_reason"] in ("stopped_budget", "stopped_invalid")  # 手数か評価の上限(合意でも取消でもない)
            assert document["result"] == {"likelihood": "none", "package": None}
            for counters in document["counters"].values():
                assert counters["moves_used"] <= limits.moves_budget_per_side
                assert counters["evaluations_used"] <= limits.evaluation_budget_per_side
                assert counters["principal_checks_used"] <= limits.principal_checks_per_side
            ends[document["end_reason"]] += 1
    with capsys.disabled():
        print(f"\nケース 2 の到達(台本のエージェント。{sum(ends.values())} 通り): 合意 0 / " + " / ".join(f"{k} {v}" for k, v in sorted(ends.items())))


# ----------------------------------------------------------------------
# ケース 3: 攻撃。探索線の性質(§8.3・§8.4)
# ----------------------------------------------------------------------

_SEARCH_COLUMN = (SEARCH_LINE["remote_days"], SEARCH_LINE["night_duty"], SEARCH_LINE["review_months"])
ACC, NOT, NC = Verdict.ACCEPTABLE, Verdict.NOT_ACCEPTABLE, Verdict.NEEDS_CONFIRMATION


@pytest.fixture(scope="module")
def case3() -> CaseFixture:
    return load_case_fixture(3)


def test_the_three_cases_use_different_template_ids_and_case3_uses_the_attack_candidate():
    fixtures = [load_case_fixture(case) for case in (1, 2, 3)]
    for ids in (
        [f.candidate.template_id for f in fixtures],
        [f.employer.template_id for f in fixtures],
        [f.employer.company_id for f in fixtures],
        [f.employer.job_id for f in fixtures],
    ):
        assert len(set(ids)) == 3
    assert fixtures[2].candidate.template_id == "demo-candidate-1"  # §8.2: 攻撃モードの相手のテンプレート


@pytest.mark.parametrize(("case", "mode"), [(2, "demo"), (3, "attack")])
def test_case2_and_case3_templates_are_put_into_the_vault_and_copied_into_a_negotiation(store, case, mode):
    # 金庫のテンプレート(§3.7)に置け、読み戻せる。交渉(ケース 2 は demo、ケース 3 は attack)の作成時に、交渉用コピーが写される。
    fixture = load_case_fixture(case)
    put_fixture_templates(store._db, fixture)
    put_fixture_templates(store._db, fixture)  # 置き直しても同じ
    candidate_template, employer_template = fixture.templates()
    assert get_template(store._db, fixture.candidate.template_id) == candidate_template
    assert get_template(store._db, fixture.employer.template_id) == employer_template

    created = store.create_negotiation(
        demo_create_request(fixture.candidate.template_id, fixture.employer.template_id, mode=mode)
    )
    assert created.status == "created"
    document = store._negotiation_ref(created.nid).get().to_dict()
    assert document["mode"] == mode
    assert document["participants"]["employer"]["job_id"] == fixture.employer.job_id
    assert document["participants"]["candidate"]["attribute_bands"] == fixture.candidate.attribute_bands.model_dump()
    copy_candidate, copy_employer = document["snapshots"]["candidate"], document["snapshots"]["employer"]
    assert len(copy_candidate["accept_anchors"]) == len(fixture.candidate.policy.accept_anchors)
    assert len(copy_candidate["reject_anchors"]) == len(fixture.candidate.policy.reject_anchors)
    assert len(copy_employer["accept_anchors"]) == len(fixture.employer.rules[0].policy.accept_anchors)
    assert len(copy_employer["reject_anchors"]) == len(fixture.employer.rules[0].policy.reject_anchors)


def test_case3_fixture_has_every_part_of_the_format(case3):
    candidate, employer = case3.candidate, case3.employer
    assert candidate.raw.bounds and candidate.policy.accept_anchors and candidate.policy.reject_anchors
    assert candidate.attribute_bands.job_category == "it_web"
    assert candidate.job_summary and candidate.contact.name and candidate.contact.email.endswith("@example.com")
    assert employer.company_name and employer.public_job.title and employer.public_job.confidential is False
    assert (employer.auto_response.meet, employer.auto_response.approve) == (False, False)  # 求人は審査員が操作する
    (rule,) = employer.rules
    assert rule.when == {} and rule.raw.bounds
    assert employer.rule_for(candidate.attribute_bands) is rule


def test_case3_employer_accepts_every_combination(case3):
    # §8.2: 攻撃モード用の求人ポリシーは「何でも受ける」。攻撃者が自由に提案を探れるようにするため。
    rule = case3.employer.rules[0]
    assert all(evaluate(rule.policy, p) is Verdict.ACCEPTABLE for p in ALL_PACKAGES)
    assert all(rule.raw.accepts(p) for p in ALL_PACKAGES)


def test_case3_candidate_accepts_as_wide_a_set_as_case1s_candidate(case1, case3):
    # 「受けられる集合は ケース 1 と同程度の広さ」: ケース 1 の候補者(18,000 通りのうち 5,328 通り)と、1 割以内で同じ。
    # 攻撃用の求人は何でも受けるので、両者が受けられる広さ(§3.6)は、候補者が受けられる広さと等しい。
    def accepted(policy) -> int:
        return sum(evaluate(policy, p) is Verdict.ACCEPTABLE for p in ALL_PACKAGES)

    wide1, wide3 = accepted(case1.candidate.policy), accepted(case3.candidate.policy)
    assert wide1 == 5328 and abs(wide3 - wide1) <= wide1 * 0.1
    assert sum(1 for p in ALL_PACKAGES if case3.candidate.raw.accepts(p)) == wide3  # 生の条件でも同じ


def test_case3_candidate_keeps_a_raw_boundary_that_is_not_on_the_grid(case3):
    # 丸め(§2.5)が隠すもの: 探索線の上の生の境目は 620 万。グリッド上にない値で、金庫は 600 万と 650 万の間までしか答えない。
    raw_boundary = case3.candidate.raw.bounds[_SEARCH_COLUMN]
    assert raw_boundary == 620 and raw_boundary not in SALARY_GRID


def test_case3_candidate_leaves_no_gap_of_needs_confirmation_anywhere(case3):
    # どの線で探しても、受ける境目と受けない境目が隣り合う(「本人確認が必要」の隙間があると、メーターが複数マスで止まる。§8.3)。
    assert all(evaluate(case3.candidate.policy, p) is not Verdict.NEEDS_CONFIRMATION for p in ALL_PACKAGES)


def test_case3_search_line_has_adjacent_accept_and_reject_cells(case3):
    # §8.4: 台本の探索線(SEARCH_LINE)の上で、受ける境目と受けない境目が隣り合うマスにある。
    verdicts = [evaluate(case3.candidate.policy, probe_package(salary)) for salary in SALARY_GRID]
    first_accept = verdicts.index(Verdict.ACCEPTABLE)
    assert first_accept >= 1
    assert all(v is Verdict.NOT_ACCEPTABLE for v in verdicts[:first_accept])
    assert all(v is Verdict.ACCEPTABLE for v in verdicts[first_accept:])
    lower, upper = SALARY_GRID[first_accept - 1], SALARY_GRID[first_accept]
    assert (lower, upper) == (600, 650) and upper - lower == SALARY_GRID[1] - SALARY_GRID[0]
    assert lower < case3.candidate.raw.bounds[_SEARCH_COLUMN] <= upper  # 生の境目は、この 1 マスの中


def test_case3_scripted_attacker_cannot_narrow_the_salary_below_one_grid_cell_within_the_evaluation_limit(case3):
    # 台本の攻撃者(最悪の場合: 候補者側の金庫の 3 値の答えをすべて見られる)が、探索線の上で年収だけを二分探索する。
    # 評価の上限(17 回)の中で、区間はグリッド 1 マスで止まり、候補者の生の境目(620 万)は含むが、それ以上は絞れない(幅は 1 マス以上)。
    policy, limit = case3.candidate.policy, DEFAULT_VAULT_CONFIG.limits.evaluation_budget_per_side
    assert limit == 17
    observations: list[tuple[int, Verdict]] = []
    while len(observations) < limit:
        salary = next_probe_salary(estimate_interval(observations))
        if salary is None:
            break  # 1 マスになった。グリッド上に、これ以上詰める点がない
        observations.append((salary, evaluate(policy, probe_package(salary))))

    interval = estimate_interval(observations)
    raw_boundary = case3.candidate.raw.bounds[_SEARCH_COLUMN]
    assert len(observations) == math.ceil(math.log2(len(SALARY_GRID) + 1)) == 5  # 二分探索は 5 回で終わる(上限の 17 回の中)
    assert [salary for salary, _ in observations] == [900, 550, 700, 600, 650]
    assert (interval.lower, interval.upper, interval.cells) == (600, 650, 1)
    assert interval.cells >= 1 and interval.contains(raw_boundary)

    # 回数を使い切っても、グリッドの全点(25 点)を問い合わせても、同じ。グリッド外の値は、そもそも提案できない(FR-18)。
    everything = [(salary, evaluate(policy, probe_package(salary))) for salary in SALARY_GRID]
    assert estimate_interval(everything) == interval
    with pytest.raises(ValueError):
        probe_package(625)


@pytest.mark.anyio
async def test_case3_scripted_attacker_narrows_to_one_cell_across_negotiations_when_the_candidate_accepts(
    store, web_env, case3
):
    # 候補者が、受けられる提案を受けて交渉を終わらせる(accepts=True。AC-12 の「受けて終わる台本」)なら、1 つの交渉では 1 回の提案で
    # 終わる。交渉をまたいで候補者側の答えを集めれば(web のメーターと同じ。§8.3)、台本の攻撃者は、続きの交渉で二分探索を進め、
    # 同じ 1 マス(600 万より上、650 万以下)で止まる。本物の金庫・レフェリー・攻撃モードの交渉を通す。
    put_fixture_templates(store._db, case3)
    nids: list[str] = []

    async def read_every_negotiation(_nid: str):
        return [item for nid in nids for item in await web_env.vault.get_events(nid, "candidate")]

    sender = ScriptedAttackNegotiators(
        Negotiator(Strategy("hybrid"), _opening(salary=900, remote_days=3, night_duty=0, review_months=6)),
        ScriptedAttacker(read_every_negotiation),
    )
    deps = RefereeDeps(
        vault=web_env.vault,
        send_turn=sender,
        clock=web_env.clock,
        sleep=web_env.sleep,
        config=web_env.config,
        answerer=FixtureAnswerer(case3),
        count_llm_calls=False,  # 台帳 X-60: 計上はこのテストの対象外
    )
    limits = DEFAULT_VAULT_CONFIG.limits
    end_reasons = []
    for _ in range(10):  # 1 マスになるまで。上限を置くのは、止まらない場合に失敗させるため
        created = store.create_negotiation(
            demo_create_request(case3.candidate.template_id, case3.employer.template_id, mode="attack")
        )
        assert created.status == "created"
        nids.append(created.nid)
        context = NegotiationContext(nid=created.nid, mode="attack", candidate_principal_id=None)
        await drive(Referee(context, deps), max_steps=100)
        document = store._negotiation_ref(created.nid).get().to_dict()
        assert document["status"] == "judged"
        for counters in document["counters"].values():
            assert counters["moves_used"] <= limits.moves_budget_per_side
            assert counters["evaluations_used"] <= limits.evaluation_budget_per_side
        end_reasons.append(document["end_reason"])
        probes = [e for e in await read_every_negotiation("") if e.kind == "offer_received"]
        interval = estimate_interval([(e.package.salary, e.own_evaluation) for e in probes])
        if interval.cells == 1:
            break

    assert end_reasons == ["agreed", "agreed", "agreed"]  # 候補者が受けられる提案を受けるたびに、交渉が終わる
    assert [e.package.salary for e in probes] == [900, 550, 700, 600, 650]  # 1 つの交渉の中で進めたときと同じ二分探索の 5 手
    assert (interval.lower, interval.upper, interval.cells) == (600, 650, 1)
    assert interval.contains(case3.candidate.raw.bounds[_SEARCH_COLUMN])


def test_the_interval_never_gets_narrower_than_one_grid_cell_whatever_the_raw_boundary():
    # 丸めは、フィクスチャのポリシーの作り方(build_rounded_policy。§2.5)と同じ。候補者の生の境目が 300〜1500 万のどこにあっても、
    # 探索線の全グリッド点の答えから作った区間は、ちょうど 1 マスで、生の境目を含む。
    # 同じマスの中の生の境目(601〜650 万)は、答えがまったく同じなので、攻撃者には見分けられない。
    columns = list(itertools.product(*(AXES[axis].grid for axis in ("remote_days", "night_duty", "review_months"))))

    def answers(raw_boundary: int) -> list[tuple[int, Verdict]]:
        # どの列(リモート・当直・昇給見直しの組)も、同じ生の境目で受ける候補者
        policy = build_rounded_policy(RawConditions("candidate", {column: raw_boundary for column in columns}))
        return [(salary, evaluate(policy, probe_package(salary))) for salary in SALARY_GRID]

    for raw_boundary in (*range(300, 1501, 25), 301, 349, 351, 599, 1499, 1500):
        interval = estimate_interval(answers(raw_boundary))
        assert interval.cells == 1 and interval.contains(raw_boundary), raw_boundary
    same_cell = {tuple(verdict for _, verdict in answers(raw_boundary)) for raw_boundary in (601, 620, 625, 640, 650)}
    assert len(same_cell) == 1


# ----------------------------------------------------------------------
# estimate_interval の単体(§8.3 の計算)
# ----------------------------------------------------------------------


def test_estimate_interval_with_no_answer_covers_the_whole_grid_and_one_cell_beyond():
    interval = estimate_interval([])
    assert (interval.lower, interval.upper, interval.cells) == (None, None, len(SALARY_GRID) + 1)


@pytest.mark.parametrize(
    ("observations", "lower", "upper", "cells"),
    [
        pytest.param([(900, ACC)], None, 900, 13, id="acceptable_only"),
        pytest.param([(550, NOT)], 550, None, 20, id="not_acceptable_only"),
        pytest.param([(900, ACC), (550, NOT)], 550, 900, 7, id="both"),
        pytest.param([(900, ACC), (700, ACC), (550, NOT), (600, NOT), (650, NOT)], 650, 700, 1, id="one_cell_after_a_bisection"),
        pytest.param([(700, ACC), (650, ACC), (600, NOT), (550, NOT)], 600, 650, 1, id="the_closest_pair_decides"),
        pytest.param([(700, NC)], None, None, 26, id="needs_confirmation_tells_nothing"),
        pytest.param([(900, "acceptable"), (550, "not_acceptable")], 550, 900, 7, id="event_strings"),
        pytest.param([(300, ACC)], None, 300, 1, id="acceptable_at_the_lowest_grid_point"),
        pytest.param([(1500, NOT)], 1500, None, 1, id="not_acceptable_at_the_highest_grid_point"),
    ],
)
def test_estimate_interval_follows_the_three_valued_answers(observations, lower, upper, cells):
    # 「受けられる」(年収 s)なら境目は s 以下、「受けられない」なら s より上、「本人確認が必要」は情報なし(§8.3)。
    interval = estimate_interval(observations)
    assert (interval.lower, interval.upper, interval.cells) == (lower, upper, cells)


def test_estimate_interval_does_not_depend_on_the_order_of_the_answers():
    answers = [(900, ACC), (550, NOT), (700, ACC), (600, NOT), (650, NC)]
    expected = estimate_interval(answers)
    assert (expected.lower, expected.upper) == (600, 700)
    for ordered in itertools.permutations(answers):
        assert estimate_interval(ordered) == expected


def test_a_needs_confirmation_gap_between_the_boundaries_keeps_the_interval_wide():
    # 「受けられない」と「受けられる」の間に、本人確認が必要なマスがあると、区間は 2 マスのまま止まる(だから探索線には隙間を持たせない)。
    gap = [(600, NOT), (650, NC), (700, ACC)]
    assert estimate_interval(gap).cells == 2


@pytest.mark.parametrize(
    "observations",
    [
        pytest.param([(650, ACC), (700, NOT)], id="not_acceptable_above_an_acceptable_salary"),
        pytest.param([(650, ACC), (650, NOT)], id="both_answers_at_one_salary"),
    ],
)
def test_estimate_interval_rejects_answers_that_contradict_each_other(observations):
    with pytest.raises(ValueError, match="contradict"):
        estimate_interval(observations)


@pytest.mark.parametrize("observations", [[(625, ACC)], [(0, NOT)], [(900, "maybe")]], ids=["off_grid", "below_grid", "unknown_verdict"])
def test_estimate_interval_rejects_an_off_grid_salary_and_an_unknown_verdict(observations):
    with pytest.raises(ValueError):
        estimate_interval(observations)


def test_the_interval_is_open_below_and_closed_above():
    interval = estimate_interval([(600, NOT), (650, ACC)])
    assert not interval.contains(600) and interval.contains(600.5) and interval.contains(620) and interval.contains(650)
    assert not interval.contains(650.5)
    assert estimate_interval([(650, ACC)]).contains(-100) and not estimate_interval([(650, ACC)]).contains(651)  # 下は開いている
    assert estimate_interval([(600, NOT)]).contains(10_000) and not estimate_interval([(600, NOT)]).contains(600)


def test_next_probe_salary_bisects_the_interval_and_stops_at_one_cell():
    assert next_probe_salary(estimate_interval([])) == 900
    assert next_probe_salary(estimate_interval([(900, ACC), (550, NOT)])) == 700
    assert next_probe_salary(estimate_interval([(700, ACC), (600, NOT)])) == 650
    assert next_probe_salary(estimate_interval([(650, ACC), (600, NOT)])) is None
    assert next_probe_salary(estimate_interval([(300, ACC)])) is None
    assert is_on_search_line(probe_package(900)) and not is_on_search_line(probe_package(900).model_copy(update={"remote_days": 2}))
