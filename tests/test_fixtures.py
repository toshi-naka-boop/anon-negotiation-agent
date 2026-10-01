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
  台本のエージェント(tests/scripted_negotiators.py。Gemini と同じく TurnInput だけを見る)。
- 36 通りの始め方(§8.4。候補者の最初の手 6 通り × 求人の最初の手 6 通り)× 探し方(7 巡目の表の行)。
  上限は config/params.toml の値のまま(側ごとに手数 6・評価 16・途中確認 1)。
- 合否に入れる探し方(1・2・5 行目)は、それぞれ 36 通り中 34 通り以上が合意(judged・agreed)に届くこと。
  記録だけの探し方(3・4・6 行目と、5 行目の向きを入れ替えたもの)の結果は、表にして出力する(合否に入れない)。
"""

import itertools
from collections import Counter
from dataclasses import dataclass

import pytest

from negotiation_core import (
    Package,
    Verdict,
    contained_in,
    evaluate,
    iter_all_packages,
    satisfies,
)

from scripted_negotiators import Negotiator, ScriptedNegotiators, Strategy
from vault.config import DEFAULT_VAULT_CONFIG
from vault.fixtures import FIXTURES_DIRECTORY, CaseFixture, load_case_fixture, put_fixture_templates
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
    # 追記の後は、同じ組み合わせの評価が、生の条件の答えどおりに決まる。ケース 1 の丸め済みポリシーは、判定が決まらない
    # 組み合わせを持たないので、聞かれる側のポリシーだけを「何も決まっていない」ものに替えて確かめる。
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
    ask_check_end = (move_dict("ask_principal", package), move_dict("check", package), move_dict("end"))
    if asking_side == "candidate":
        env.agents.script("candidate", *ask_check_end)
    else:
        env.agents.script("candidate", move_dict("propose", package))
        env.agents.script("employer", *ask_check_end)
    await drive(env.referee(created.nid))

    events = store.get_events(created.nid, asking_side)
    answer = next(e for e in events if e.kind == "principal_answer")
    assert answer.answer == expected_answer
    check = next(e for e in events if e.kind == "check")
    expected_verdict = Verdict.ACCEPTABLE if expected_answer == "accept" else Verdict.NOT_ACCEPTABLE
    assert Verdict(check.own_evaluation) is expected_verdict


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
