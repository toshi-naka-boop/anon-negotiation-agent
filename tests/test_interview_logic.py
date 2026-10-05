"""面談(design.md §5)の決定的なロジック(LLM なし。web/interview の部品)。

- 設問テンプレート(fixtures/interview_templates.toml。暫定・U-01)と設定([web.interview])の検証
- プロフィール → 属性帯(§2.6)
- 年収の換算式と前提の文(§5 の 2)
- パッケージ二択の生成(5〜8 組・外した軸の見せ方。§5 の 4・§2.4)
- 二択の回答・発言 → アンカー(§2.3・§2.4。複数の軸を外したときを含む。DV-05・DV-09 と同じ考え方)
- 平文の確認文(§5 の 6)、受けるアンカーが 0 件のときの警告の元(I-1)、矛盾、「最悪ここまで」(§5 の 7)、送信の形(§5 の 9)
- 企業の一覧(§5 の 8)
"""

import copy
import dataclasses
import itertools
import tomllib

import pytest

from negotiation_core import (
    ATTRIBUTE_BANDS,
    AXES,
    AXIS_KEYS,
    PartialStatement,
    Verdict,
    convert_statement_to_anchor,
    convert_two_choice_answer_to_anchor,
    evaluate,
    iter_all_packages,
    round_anchor,
    worst_value,
)
from negotiation_core.policy import Package, Policy
from web.config import DEFAULT_WEB_CONFIG
from web.interview.anchors import (
    AnchorEntry,
    ChoiceAnswer,
    StatementRecord,
    count_by_polarity,
    derive_entries,
    find_conflicts,
    rounded_anchor,
    to_submit_request,
    worst_case_view,
)
from web.interview.choices import generate_pairs, removed_axes_question, select_pair_templates
from web.interview.companies import list_companies
from web.interview.config import DEFAULT_INTERVIEW_CONFIG, load_interview_config
from web.interview.profile import ProfileError, experience_band, job_category, profile_to_bands, region_block
from web.interview.salary import (
    SalaryBasis,
    SalaryConversionError,
    nearest_grid_index,
    normalize_salary,
)
from web.interview.sentences import describe_anchor, describe_offer, describe_statement, display_value
from web.interview.statements import (
    choice_to_raw,
    parse_constraint_list,
    shown_values,
    statement_skip_reason,
    statement_to_raw,
)
from web.interview.templates import (
    DISCRETE_AXIS_KEYS,
    TEMPLATES_PATH,
    InterviewTemplates,
    default_templates,
)

TEMPLATES = default_templates()
CONFIG = DEFAULT_INTERVIEW_CONFIG
ALL_PACKAGES = list(iter_all_packages())


def all_subsets(items):
    for size in range(len(items) + 1):
        yield from itertools.combinations(items, size)


def basis(**overrides) -> SalaryBasis:
    values = dict(
        amount_man_yen=600,
        amount_period="annual",
        amount_kind="gross",
        bonus_included=True,
        bonus_months=0,
        fixed_overtime_man_yen_per_month=0,
    )
    values.update(overrides)
    return SalaryBasis(**values)


# ---------------------------------------------------------------------------
# 設問テンプレートと設定
# ---------------------------------------------------------------------------


def test_the_template_file_is_marked_provisional_and_agrees_with_the_vocabulary():
    # U-01: 文面は暫定。ファイルの先頭にそう明記してあり、読み込みで語彙(グリッド・軸)との一致が検証される。
    header = "\n".join(TEMPLATES_PATH.read_text(encoding="utf-8").splitlines()[:5])
    assert "U-01" in header and "暫定" in header
    assert TEMPLATES.provisional is True
    assert len(TEMPLATES.salary_questions) == 3  # 年収の正規化 3 問(FR-01)
    assert "{days}" in TEMPLATES.notice.auto_delete
    assert set(TEMPLATES.two_choice.removed_axis_phrases) == set(DISCRETE_AXIS_KEYS)
    assert TEMPLATES.reason_for_leaving.prompt and TEMPLATES.free_comment.prompt


def test_the_pair_templates_have_enough_salary_only_pairs_so_that_removing_every_axis_still_leaves_five():
    salary_only = [pair for pair in TEMPLATES.pairs if not pair.traded_axes]
    trade_offs = [pair for pair in TEMPLATES.pairs if pair.traded_axes]
    assert len(salary_only) >= 5
    assert {axis for pair in trade_offs for axis in pair.traded_axes} == set(DISCRETE_AXIS_KEYS)  # 軸ごとにトレードオフがある


def _raw_templates() -> dict:
    with TEMPLATES_PATH.open("rb") as f:
        return tomllib.load(f)


def _broken(mutate) -> dict:
    raw = copy.deepcopy(_raw_templates())
    mutate(raw)
    return raw


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda raw: raw["pairs"][0]["a"].update(remote_days=7), id="off-grid remote_days"),
        pytest.param(lambda raw: raw["pairs"][0]["a"].update(training="sometimes"), id="unknown categorical value"),
        pytest.param(lambda raw: raw["two_choice"]["removed_axis_phrases"].pop("night_duty"), id="missing removed-axis phrase"),
        pytest.param(
            lambda raw: raw["two_choice"]["removed_axis_phrases"].update(night_duty="当直があっても"), id="ordered phrase without {value}"
        ),
        pytest.param(lambda raw: raw.update(pairs=raw["pairs"][:-1]), id="fewer than five salary-only pairs"),
        pytest.param(lambda raw: raw["pairs"][1].update(id="remote"), id="duplicate pair id"),
        pytest.param(lambda raw: raw.update(salary_questions=raw["salary_questions"][:2]), id="two salary questions"),
        pytest.param(lambda raw: raw["region_prefectures"]["kanto"].pop(), id="missing prefecture"),
        pytest.param(lambda raw: raw["labels"]["job_category"].pop("other"), id="missing label"),
        pytest.param(lambda raw: raw["pairs"][0].update(b=raw["pairs"][0]["a"]), id="identical options"),
        pytest.param(lambda raw: raw.update(unknown="x"), id="unknown key"),
    ],
)
def test_a_template_file_that_disagrees_with_the_design_is_rejected(mutate):
    with pytest.raises(ValueError):
        InterviewTemplates.model_validate(_broken(mutate))


def test_the_interview_config_is_read_from_params_toml_and_validated():
    config = load_interview_config()
    assert config == CONFIG
    assert config.max_request_body_bytes == 32 * 1024  # C-49
    assert config.max_output_tokens == 2048  # C-49
    assert (config.max_lifetime_seconds, config.max_concurrent_per_client) == (10800, 3)  # 台帳 C-69・X-87(v23)
    assert 5 <= config.min_answered_pairs <= config.choice_pairs <= 8
    for changes in (
        {"max_lifetime_seconds": 3599},  # アイドルの寿命(3600)より短い絶対の寿命は、意味がない
        {"max_concurrent_per_client": 0},
        {"max_output_tokens": 100},
        {"max_output_tokens": 9999},
        {"choice_pairs": 9},
        {"min_answered_pairs": 4},
        {"min_answered_pairs": 7, "choice_pairs": 6},
        {"thinking_level": "EXTREME"},
        {"net_to_gross_ratio": 1.5},
        {"experience_band_upper_bounds": (3.0, 5.0)},
        {"experience_band_upper_bounds": (5.0, 3.0, 10.0)},
    ):
        with pytest.raises(ValueError):
            dataclasses.replace(CONFIG, **changes)


# ---------------------------------------------------------------------------
# プロフィール → 属性帯(§2.6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("years", "band"),
    [(0, "under_3y"), (2.99, "under_3y"), (3, "3_to_5y"), (4.9, "3_to_5y"), (5, "5_to_10y"), (9.99, "5_to_10y"), (10, "10y_plus"), (45, "10y_plus")],
)
def test_experience_years_are_turned_into_bands_at_the_configured_boundaries(years, band):
    assert experience_band(years, CONFIG.experience_band_upper_bounds) == band


def test_every_experience_band_in_the_grid_can_be_reached():
    reached = {experience_band(years, CONFIG.experience_band_upper_bounds) for years in (0, 3, 5, 10)}
    assert reached == set(ATTRIBUTE_BANDS["experience_band"].grid)


def test_all_47_prefectures_map_to_a_region_block_and_short_names_are_accepted():
    prefectures = [name for names in TEMPLATES.region_prefectures.values() for name in names]
    assert len(prefectures) == 47
    for block, names in TEMPLATES.region_prefectures.items():
        for name in names:
            assert region_block(name, TEMPLATES.region_prefectures) == block
    assert region_block("東京", TEMPLATES.region_prefectures) == "kanto"
    assert region_block("京都", TEMPLATES.region_prefectures) == "kinki"
    assert region_block(" 大阪府 ", TEMPLATES.region_prefectures) == "kinki"
    assert region_block("北海道", TEMPLATES.region_prefectures) == "hokkaido_tohoku"
    with pytest.raises(ProfileError) as excinfo:
        region_block("アトランティス", TEMPLATES.region_prefectures)
    assert excinfo.value.code == "unknown_region"


def test_the_profile_becomes_bands_only_and_unknown_jobs_are_refused():
    bands = profile_to_bands(
        experience_years=7.3141,
        prefecture="東京都",
        job="it_web",
        upper_bounds=CONFIG.experience_band_upper_bounds,
        region_prefectures=TEMPLATES.region_prefectures,
    )
    assert bands.model_dump() == {"experience_band": "5_to_10y", "region_block": "kanto", "job_category": "it_web"}
    assert "7.3141" not in bands.model_dump_json() and "東京" not in bands.model_dump_json()  # 正確な値は、帯に残らない
    assert job_category("medical_welfare") == "medical_welfare"
    with pytest.raises(ProfileError) as excinfo:
        job_category("astronaut")
    assert excinfo.value.code == "unknown_job_category"


# ---------------------------------------------------------------------------
# 年収の換算(§5 の 2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        # 年収 600(額面・賞与込み)から、固定残業代 月 3 万円(年 36 万円)を除く
        (dict(fixed_overtime_man_yen_per_month=3, bonus_months=4), 564),
        # 年収 480(賞与別)に、年収 ÷ 12 × 2 か月 = 80 を足す
        (dict(amount_man_yen=480, bonus_included=False, bonus_months=2), 560),
        # 月収 40 万円 × 12 + 賞与 40 × 4 = 640
        (dict(amount_man_yen=40, amount_period="monthly", bonus_included=False, bonus_months=4), 640),
        # 月収の手取り 28 万円 → 額面 35 万円(割合 0.8)。35 × 12 + 35 × 2 = 490
        (dict(amount_man_yen=28, amount_period="monthly", amount_kind="net", bonus_included=False, bonus_months=2), 490),
        # 年収の手取り 480 万円(賞与込み)→ 額面 600 万円
        (dict(amount_man_yen=480, amount_kind="net"), 600),
        # 月収 50 万円(賞与なし)から、固定残業代 月 8 万円(年 96 万円)を除く: 600 − 96
        (dict(amount_man_yen=50, amount_period="monthly", bonus_included=False, fixed_overtime_man_yen_per_month=8), 504),
    ],
)
def test_the_comparison_basis_salary_is_gross_annual_total_with_bonus_minus_fixed_overtime(overrides, expected):
    normalized = normalize_salary(basis(**overrides), 0.8)
    assert normalized.man_yen == pytest.approx(expected)
    assert f"{expected:g}" in normalized.formula  # 換算式に、結果の数字が入っている
    assert normalized.formula.startswith("比較基準年収 =")
    assert normalized.assumptions and all(isinstance(line, str) and line for line in normalized.assumptions)


def test_the_assumption_sentences_say_how_each_answer_was_read():
    net = normalize_salary(basis(amount_man_yen=28, amount_period="monthly", amount_kind="net", bonus_included=False, bonus_months=2), 0.8)
    assert any("手取り" in line and "80%" in line for line in net.assumptions)
    assert any("月収" in line and "賞与" in line for line in net.assumptions)
    without_overtime = normalize_salary(basis(), 0.8)
    assert any("固定残業代は、ないものとして" in line for line in without_overtime.assumptions)
    with_overtime = normalize_salary(basis(fixed_overtime_man_yen_per_month=3), 0.8)
    assert any("月 3 万円" in line and "年 36 万円" in line for line in with_overtime.assumptions)
    not_included = normalize_salary(basis(amount_man_yen=480, bonus_included=False, bonus_months=2), 0.8)
    assert any("含まれていない" in line for line in not_included.assumptions)


def test_a_result_that_is_not_positive_is_refused_and_out_of_grid_results_are_noted():
    with pytest.raises(SalaryConversionError):
        normalize_salary(basis(amount_man_yen=100, fixed_overtime_man_yen_per_month=10), 0.8)  # 100 − 120 < 0
    low = normalize_salary(basis(amount_man_yen=250), 0.8)
    high = normalize_salary(basis(amount_man_yen=2000), 0.8)
    assert any("300" in line for line in low.assumptions) and any("1500" in line for line in high.assumptions)
    assert not any("範囲" in line for line in normalize_salary(basis(), 0.8).assumptions)


@pytest.mark.parametrize(
    "bad",
    [
        dict(amount_man_yen=0),
        dict(amount_man_yen=-5),
        dict(amount_period="weekly"),
        dict(amount_kind="unknown"),
        dict(bonus_months=-1),
        dict(bonus_months=30),
        dict(fixed_overtime_man_yen_per_month=-1),
        dict(extra_field="x"),
    ],
)
def test_the_salary_basis_type_rejects_values_outside_its_definition(bad):
    with pytest.raises(ValueError):
        basis(**bad)


@pytest.mark.parametrize(
    ("value", "grid_value"), [(564, 550), (623, 600), (625, 650), (650, 650), (100, 300), (5000, 1500), (1475, 1500)]
)
def test_the_stated_salary_is_moved_to_the_nearest_grid_point_and_ties_go_up(value, grid_value):
    assert AXES["salary"].grid[nearest_grid_index(value)] == grid_value


# ---------------------------------------------------------------------------
# パッケージ二択の生成(§5 の 4・§2.4)
# ---------------------------------------------------------------------------


def _pairs(base=650, removed=()):
    return generate_pairs(base, removed, TEMPLATES, CONFIG.choice_pairs)


def test_five_to_eight_pairs_are_made_around_the_stated_salary_without_removed_axes():
    pairs = _pairs(650)
    assert CONFIG.choice_pairs == len(pairs) and 5 <= len(pairs) <= 8
    assert [pair.id for pair in pairs] == [pair.id for pair in TEMPLATES.pairs[: CONFIG.choice_pairs]]
    salaries = {values["salary"] for pair in pairs for values in (pair.a, pair.b)}
    assert min(salaries) >= 650 - 150 and max(salaries) <= 650 + 150  # 本人が言った年収の周辺
    for pair in pairs:
        assert pair.a != pair.b
        for values in (pair.a, pair.b):
            assert set(values) == set(AXIS_KEYS)
            for axis in AXIS_KEYS:
                assert values[axis] in AXES[axis].grid or values[axis] == "*"
    assert _pairs(650) == _pairs(650)  # 決定的


@pytest.mark.parametrize("base", [100, 300, 564, 650, 1475, 1500, 5000])
def test_every_choice_of_removed_axes_still_gives_at_least_five_pairs_and_all_values_stay_on_the_grid(base):
    for removed in all_subsets(DISCRETE_AXIS_KEYS):
        pairs = _pairs(base, removed)
        assert 5 <= len(pairs) <= 8, removed
        assert len({pair.id for pair in pairs}) == len(pairs)
        for pair in pairs:
            assert pair.a != pair.b, (removed, pair.id)
            for values in (pair.a, pair.b):
                assert values["salary"] in AXES["salary"].grid  # グリッドの端でも、収まる位置まで土台をずらす
                for axis in removed:  # 外した軸は、どの組でも最も悪い値・どちらでも
                    assert values[axis] == (worst_value(axis, "candidate") if AXES[axis].kind == "numeric" else "*")


def test_pairs_near_the_grid_edges_shift_the_base_inwards_instead_of_collapsing():
    top = _pairs(1500, ("remote_days", "night_duty", "review_months", "training", "side_job", "start"))
    bottom = _pairs(300, ())
    assert max(values["salary"] for pair in top for values in (pair.a, pair.b)) == 1500
    assert all(pair.a["salary"] != pair.b["salary"] for pair in top)  # 年収だけの組は、2 つの年収が重ならない
    assert min(values["salary"] for pair in bottom for values in (pair.a, pair.b)) >= 300


def test_removing_all_axes_leaves_exactly_the_five_salary_only_pairs():
    removed = DISCRETE_AXIS_KEYS
    chosen = select_pair_templates(removed, TEMPLATES, CONFIG.choice_pairs)
    assert [pair.id for pair in chosen] == [pair.id for pair in TEMPLATES.pairs if not pair.traded_axes][:5]
    assert all(not pair.traded_axes for pair in chosen)


def test_a_pair_that_trades_a_removed_axis_is_not_asked():
    ids = {pair.id for pair in _pairs(650, ("night_duty",))}
    assert "night_duty" not in ids and "remote" in ids
    ids = {pair.id for pair in _pairs(650, ("training", "side_job", "start"))}
    assert not ids & {"training", "side_job", "start"}


def test_the_question_names_the_removed_axes_in_the_template_wording():
    assert removed_axes_question((), TEMPLATES) == "この条件なら行きますか?"
    assert removed_axes_question(("night_duty",), TEMPLATES) == "当直が月 8 回でも、この条件なら行きますか?"  # §2.4 の例
    assert removed_axes_question(("training",), TEMPLATES) == "研修があってもなくても、この条件なら行きますか?"  # §2.4 の例
    assert removed_axes_question(("training", "night_duty"), TEMPLATES) == "当直が月 8 回でも、研修があってもなくても、この条件なら行きますか?"
    assert removed_axes_question(("remote_days",), TEMPLATES).startswith("リモートが週 0 日でも")
    assert removed_axes_question(("review_months",), TEMPLATES).startswith("昇給見直しが 12 か月後でも")


def test_an_option_is_described_without_removed_axes_and_without_unasked_categorical_axes():
    pair = next(pair for pair in _pairs(650, ("night_duty",)) if pair.id == "remote")
    assert describe_offer(pair.a, ("night_duty",)) == "年収 750 万円・フル出社・昇給見直し 12 か月後"
    assert describe_offer(pair.b, ("night_duty",)) == "年収 650 万円・週 3 日リモート・昇給見直し 12 か月後"
    assert display_value("night_duty", pair.a["night_duty"]) == "月 8 回"  # 表では、外した軸も最も悪い値で見せる
    assert display_value("training", "*") == "どちらでも" and display_value("salary", 650) == "650 万円"
    assert describe_offer(next(p for p in _pairs(650) if p.id == "training").a, ()).endswith("研修なし")


# ---------------------------------------------------------------------------
# 二択の回答 → アンカー(§2.4。複数の軸を外したときを含む)
# ---------------------------------------------------------------------------


def test_go_is_an_accept_anchor_no_go_is_a_reject_anchor_and_undecided_is_nothing():
    values = _pairs(650)[0].a
    go = choice_to_raw(values, "go", ())
    no_go = choice_to_raw(values, "no_go", ())
    assert go[0] == "accept" and no_go[0] == "reject" and go[1] == no_go[1] == values
    assert choice_to_raw(values, "undecided", ()) is None


def test_with_removed_axes_go_is_neutral_on_them_and_no_go_is_not_saved():
    removed = ("night_duty", "training")
    values = _pairs(650, removed)[0].a
    polarity, raw = choice_to_raw(values, "go", removed)
    assert polarity == "accept"
    assert raw["night_duty"] == worst_value("night_duty", "candidate") and raw["training"] == "*"  # x について中立(§2.2)
    assert choice_to_raw(values, "no_go", removed) is None  # 外した軸があるときの「行かない」は保存しない(§2.4)
    assert choice_to_raw(values, "undecided", removed) is None


@pytest.mark.parametrize("axis", DISCRETE_AXIS_KEYS)
@pytest.mark.parametrize("response", ["go", "no_go", "undecided"])
def test_with_one_removed_axis_the_result_is_the_same_as_negotiation_cores_rule(axis, response):
    # 複数の軸を外すために足した変換は、軸を 1 つだけ外すとき、negotiation_core の規則(DV-05・DV-09)と同じ結果になる。
    for pair in _pairs(650, ()):
        for values in (pair.a, pair.b):
            core = convert_two_choice_answer_to_anchor(dict(values), response, "candidate", axis)
            mine = choice_to_raw(shown_values(values, (axis,)), response, (axis,))
            if core is None:
                assert mine is None
            else:
                assert mine == (core[1], core[0].model_dump())


def _principal_accepts(package: Package) -> bool:
    """単調な本人のモデル(negotiation_core とは独立): 年収 550 万以上・リモート週 1 日以上・当直は月 2 回まで。"""
    return package.salary >= 550 and package.remote_days >= 1 and package.night_duty <= 2


def _principal_answer(values: dict) -> str:
    """見せた値(外した区分軸は * )に対する本人のモデルの回答。* の軸は、どの値でも行くときだけ「行く」。"""
    wildcard_axes = [axis for axis in AXES if values[axis] == "*"]
    for completion in itertools.product(*(AXES[axis].grid for axis in wildcard_axes)):
        probe = {**values, **dict(zip(wildcard_axes, completion, strict=True))}
        if not _principal_accepts(Package(**probe)):
            return "no_go"
    return "go"


@pytest.mark.parametrize(
    "removed",
    [(), ("night_duty",), ("remote_days",), ("training",), ("night_duty", "remote_days"), ("training", "side_job", "start"), DISCRETE_AXIS_KEYS],
)
def test_two_choice_answers_never_make_the_policy_accept_what_the_principal_refuses(removed):
    # DV-05 の考え方(健全性と単調性)を、複数の軸を外したときにも: 本人のモデルで二択に答えさせ、変換したポリシーを全 18,000 通りで調べる。
    # 「受けられる」と言うなら本人も行く。「受けられない」と言うなら本人も行かない。外した軸があると、受けないアンカーは作らない。
    answers = {}
    for pair in _pairs(650, removed):
        for name, values in (("a", pair.a), ("b", pair.b)):
            answers[(pair.id, name)] = ChoiceAnswer(values=values, response=_principal_answer(values))
    derived = derive_entries(answers, [], set(), removed)
    accept = [round_anchor(e.raw, "accept", "candidate") for e in derived.entries if e.polarity == "accept"]
    reject = [round_anchor(e.raw, "reject", "candidate") for e in derived.entries if e.polarity == "reject"]
    policy = Policy(side="candidate", accept_anchors=accept, reject_anchors=reject)
    for package in ALL_PACKAGES:
        verdict = evaluate(policy, package)
        if verdict is Verdict.ACCEPTABLE:
            assert _principal_accepts(package), (removed, package)
        if verdict is Verdict.NOT_ACCEPTABLE:
            assert not _principal_accepts(package), (removed, package)
    if removed:
        assert not reject  # 外した軸があれば、二択から受けないアンカーは作らない
        for entry in derived.entries:
            for axis in removed:  # 受けるアンカーは、外した軸について中立
                assert entry.raw[axis] == (worst_value(axis, "candidate") if AXES[axis].kind == "numeric" else "*")


# ---------------------------------------------------------------------------
# 発言(自由コメント・辞めた理由) → アンカー(§2.3・§2.4)
# ---------------------------------------------------------------------------


def test_the_constraint_list_keeps_valid_statements_and_counts_the_ones_it_dropped():
    payload = {
        "statements": [
            {"polarity": "accept", "salary": 650, "remote_days": 0},  # 項目の省略・整数
            {"polarity": "reject", "night_duty": "4"},  # 数値が文字列
            {"polarity": "accept", "salary": 2000},  # 範囲外
            {"polarity": "accept", "salary": 600, "note": "x"},  # 知らない項目(説明など)は無視して、読み取りは活かす
            {"polarity": "maybe", "salary": 600},  # 列挙外
            {"polarity": "accept", "training": "sometimes"},  # 区分軸の値が列挙外
            "not an object",
            {"polarity": "reject", "start": "within_1_month"},
        ]
    }
    parsed, dropped = parse_constraint_list(payload, max_statements=10)
    assert [(s.polarity, s.salary) for s in parsed.statements] == [
        ("accept", 650.0),
        ("reject", None),
        ("accept", 600.0),
        ("reject", None),
    ]
    assert parsed.statements[1].night_duty == 4.0
    assert dropped == 4
    limited, over = parse_constraint_list({"statements": [{"polarity": "accept", "salary": 650}] * 4}, max_statements=3)
    assert len(limited.statements) == 3 and over == 1
    for malformed in (None, [], "x", {"statements": "x"}, {"other": []}):
        with pytest.raises(ValueError):
            parse_constraint_list(malformed, max_statements=10)


def test_a_statement_that_touches_a_removed_axis_or_no_axis_is_not_saved():
    on_night = PartialStatement(polarity="reject", night_duty=4)
    on_salary = PartialStatement(polarity="accept", salary=650)
    nothing = PartialStatement(polarity="reject")
    assert statement_skip_reason(on_night, ()) is None
    assert statement_skip_reason(on_night, ("night_duty",)) == "removed_axis"  # §2.4: 保存しない
    assert statement_skip_reason(on_salary, ("night_duty",)) is None  # 触れていない軸は、規則で埋めて保存する
    assert statement_skip_reason(nothing, ()) == "no_axis"  # どの軸にも触れない「行かない」は、何があっても行かない、になってしまう
    assert statement_skip_reason(PartialStatement(polarity="accept", night_duty=2, training="none"), ("training",)) == "removed_axis"


def test_the_two_sentences_of_the_design_example_become_the_anchors_of_the_design_and_do_not_contradict():
    accept = StatementRecord("comment-1-0", "comment", PartialStatement(polarity="accept", salary=650, remote_days=0))
    reject = StatementRecord("comment-1-1", "comment", PartialStatement(polarity="reject", night_duty=4))
    derived = derive_entries({}, [accept, reject], set(), ())
    assert [entry.raw for entry in derived.entries] == [
        {"salary": 650, "remote_days": 0, "night_duty": 0, "review_months": 6, "training": "*", "side_job": "*", "start": "*"},
        {"salary": 1500, "remote_days": 5, "night_duty": 4, "review_months": 6, "training": "*", "side_job": "*", "start": "*"},
    ]
    assert find_conflicts(derived.entries) == []


@pytest.mark.parametrize(
    "statement",
    [
        PartialStatement(polarity="accept", salary=623.45),
        PartialStatement(polarity="accept", salary=650, remote_days=2.5, training="available"),
        PartialStatement(polarity="reject", salary=417.25, night_duty=5),
        PartialStatement(polarity="reject", remote_days=0, side_job="allowed"),
        PartialStatement(polarity="accept", review_months=7.5, start="within_3_months"),
    ],
)
def test_the_raw_anchor_keeps_the_stated_numbers_and_rounds_to_what_negotiation_core_makes(statement):
    # 確認画面は本人の言葉のまま(丸める前)で見せる。丸めたものは、negotiation_core.convert_statement_to_anchor と同じ。
    polarity, raw = statement_to_raw(statement)
    assert polarity == statement.polarity
    for axis in ("salary", "remote_days", "night_duty", "review_months"):
        stated = getattr(statement, axis)
        if stated is not None:
            assert raw[axis] == stated
    assert round_anchor(raw, polarity, "candidate") == convert_statement_to_anchor(statement, "candidate")


def test_a_statement_that_touches_nothing_would_have_rejected_everything_which_is_why_it_is_dropped():
    # statement_skip_reason が no_axis を返す理由: 触れていない「行かない」を変換すると、すべての組み合わせを受けないアンカーになる
    everything = convert_statement_to_anchor(PartialStatement(polarity="reject"), "candidate")
    assert all(evaluate(Policy(side="candidate", reject_anchors=[everything]), p) is Verdict.NOT_ACCEPTABLE for p in ALL_PACKAGES[:500])


# ---------------------------------------------------------------------------
# 平文の確認文(§5 の 6)
# ---------------------------------------------------------------------------

ACCEPT_650 = {"salary": 650, "remote_days": 0, "night_duty": 0, "review_months": 6, "training": "*", "side_job": "*", "start": "*"}
REJECT_NIGHT_4 = {"salary": 1500, "remote_days": 5, "night_duty": 4, "review_months": 6, "training": "*", "side_job": "*", "start": "*"}


def test_the_design_example_sentences_are_made_in_the_form_of_the_design():
    assert describe_anchor("accept", ACCEPT_650) == (
        "年収 650 万円以上・フル出社でもよい・当直なし・昇給見直しは 6 か月以内なら行く(研修・副業・入職時期は問いません)"
    )
    assert describe_anchor("reject", REJECT_NIGHT_4) == "当直が月 4 回以上なら(ほかの条件がどれだけ良くても)行かない"


def test_filled_values_are_shown_so_that_the_person_can_see_how_narrow_the_reading_is():
    _, raw = statement_to_raw(PartialStatement(polarity="accept", salary=623.45))
    sentence = describe_anchor("accept", raw)
    assert "年収 623.45 万円以上" in sentence and "フルリモート" in sentence and "当直なし" in sentence  # 埋めた値(最も良い値)
    assert describe_statement(PartialStatement(polarity="accept", salary=623.45)) == "年収 623.45 万円以上なら行く"  # 読み取りは触れた軸だけ


def test_sentences_for_the_other_axes_and_edge_cases():
    base = dict(ACCEPT_650, remote_days=3, night_duty=4, review_months=12, training="available", side_job="allowed", start="within_6_months")
    assert describe_anchor("accept", base) == (
        "年収 650 万円以上・週 3 日以上のリモート・当直は月 4 回まで・昇給見直しは 12 か月以内・研修あり・副業可・入職は 6 か月以内なら行く"
    )
    reject = dict(REJECT_NIGHT_4, salary=600, remote_days=0, training="none", night_duty=0, review_months=12)
    assert describe_anchor("reject", reject) == (
        "年収 600 万円以下・フル出社・昇給見直しが 12 か月以上・研修なしなら(ほかの条件がどれだけ良くても)行かない"
    )
    best = {"salary": 1500, "remote_days": 5, "night_duty": 0, "review_months": 6, "training": "*", "side_job": "*", "start": "*"}
    assert describe_anchor("reject", best) == "どんな条件でも行かない"
    # 外した軸: 規則で埋めた値(当直なし = 最も良い値)が残っていれば、本人の意向ではないことを示す印を付ける
    filled = describe_anchor("accept", ACCEPT_650, ("night_duty",))
    assert "当直なし(外した軸。規則で埋めた値)" in filled and filled.count("外した軸") == 1
    # 二択の「行く」の中立の値(当直 月 8 回 = 最も悪い値)は、何も制約していないので、文に出さない
    neutral = describe_anchor("accept", {**ACCEPT_650, "night_duty": 8}, ("night_duty",))
    assert neutral == "年収 650 万円以上・フル出社でもよい・昇給見直しは 6 か月以内なら行く(研修・副業・入職時期は問いません)"
    assert "当直" in describe_anchor("accept", {**ACCEPT_650, "night_duty": 8})  # 外していなければ、当直 月 8 回までと出す
    assert describe_statement(PartialStatement(polarity="reject", night_duty=4)) == "当直が月 4 回以上なら行かない"


# ---------------------------------------------------------------------------
# アンカーの一覧・矛盾・0 件の警告の元・送信の形・「最悪ここまで」
# ---------------------------------------------------------------------------


def _entry(key, polarity, raw, active=True, source="comment"):
    return AnchorEntry(key=key, source=source, polarity=polarity, raw=raw, active=active)


def test_derive_entries_keeps_inactive_entries_and_reports_what_was_not_saved_because_of_removed_axes():
    removed = ("night_duty",)
    pair = _pairs(650, removed)[0]
    answers = {("remote", "a"): ChoiceAnswer(pair.a, "go"), ("remote", "b"): ChoiceAnswer(pair.b, "no_go")}
    statements = [
        StatementRecord("comment-1-0", "comment", PartialStatement(polarity="reject", night_duty=4)),
        StatementRecord("comment-1-1", "comment", PartialStatement(polarity="accept", salary=700)),
        StatementRecord("reason-2-0", "reason", PartialStatement(polarity="reject")),
    ]
    derived = derive_entries(answers, statements, {"comment-1-1"}, removed)
    assert [(e.key, e.source, e.polarity, e.active) for e in derived.entries] == [
        ("choice-remote-a", "choice", "accept", True),
        ("comment-1-1", "comment", "accept", False),  # 消した項目は、一覧に残り、付け直せる
    ]
    assert derived.not_saved == [("choice-remote-b", "choice"), ("comment-1-0", "comment")]
    assert derived.ignored_statements == 1
    assert count_by_polarity(derived.entries) == {"accept": 1, "reject": 0}  # 無効な項目は数えない


def test_contradictory_active_entries_are_found_after_rounding_and_inactive_ones_are_not_counted():
    accept = _entry("a", "accept", {**ACCEPT_650, "salary": 600})
    reject = _entry("r", "reject", {**REJECT_NIGHT_4, "night_duty": 0, "salary": 650})  # 年収 650 以下は行かない
    assert find_conflicts([accept, reject]) == [("a", "r")]
    assert find_conflicts([accept, _entry("r", "reject", reject.raw, active=False)]) == []
    # 生の値では矛盾して見えても、丸めた後(受けるは上へ・受けないは下へ)で矛盾しなければ、金庫は受け付ける
    assert find_conflicts([_entry("a", "accept", {**ACCEPT_650, "salary": 620}), _entry("r", "reject", {**REJECT_NIGHT_4, "night_duty": 0, "salary": 640})]) == []


def test_the_submit_request_rounds_on_the_web_side_and_carries_only_active_entries_and_the_bands():
    bands = profile_to_bands(
        experience_years=6, prefecture="東京都", job="it_web", upper_bounds=CONFIG.experience_band_upper_bounds,
        region_prefectures=TEMPLATES.region_prefectures,
    )
    entries = [
        _entry("a1", "accept", {**ACCEPT_650, "salary": 623.45}),
        _entry("a2", "accept", {**ACCEPT_650, "salary": 700}, active=False),
        _entry("r1", "reject", {**REJECT_NIGHT_4, "salary": 417.25, "night_duty": 0, "remote_days": 0}),
    ]
    request = to_submit_request(entries, ("night_duty",), bands).to_put_policy_request()
    assert [anchor.salary for anchor in request.policy.accept_anchors] == [650]  # 623.45 → 650(良い側)。無効な項目は含まれない
    assert [anchor.salary for anchor in request.policy.reject_anchors] == [400]  # 417.25 → 400(悪い側)
    assert request.removed_axes == ["night_duty"] and request.attribute_bands == bands
    payload = request.model_dump_json()
    assert "623.45" not in payload and "417.25" not in payload and "700" not in payload  # 金庫には、丸め済みの値しか届かない


def test_the_worst_case_view_shows_the_rounded_cells_and_marks_removed_axes():
    entries = [
        _entry("a", "accept", {**ACCEPT_650, "salary": 620, "remote_days": 3}),
        _entry("r", "reject", {**REJECT_NIGHT_4, "salary": 410, "night_duty": 0}),  # 年収 410 以下なら行かない(ほかは最も良い値)
        _entry("t", "accept", {**ACCEPT_650, "salary": 700, "training": "available"}, active=True),
        _entry("off", "accept", {**ACCEPT_650, "salary": 1000}, active=False),
    ]
    view = {item["axis"]: item for item in worst_case_view(entries, ("review_months",))}
    salary_cells = view["salary"]["cells"]
    accept_650 = next(cell for cell in salary_cells if cell["kind"] == "accept" and cell["high"] == 650)
    reject_400 = next(cell for cell in salary_cells if cell["kind"] == "reject")
    assert accept_650["text"] == "『受ける』の下限は、600 万円より上、650 万円以下のどこか"  # 設計書 §2.5・§7 の例
    assert (accept_650["low"], accept_650["low_inclusive"], accept_650["high"], accept_650["high_inclusive"]) == (600, False, 650, True)
    assert reject_400["text"] == "『受けない』の上限は、400 万円以上、450 万円未満のどこか"
    assert (reject_400["low"], reject_400["low_inclusive"], reject_400["high"], reject_400["high_inclusive"]) == (400, True, 450, False)
    assert any(cell["high"] == 700 for cell in salary_cells)
    assert all(cell["high"] != 1000 for cell in salary_cells)  # 消した項目は含めない
    assert view["review_months"]["removed"] is True and view["review_months"]["text"] == "外しています(交渉中に確認)"
    assert view["review_months"]["cells"] == []
    assert [cell["text"] for cell in view["remote_days"]["cells"]] == ["週 3 日以上のリモート"]  # 受けるアンカーの最も悪い値(0)と、受けないの最も良い値は略す
    assert [cell["text"] for cell in view["training"]["cells"]] == ["研修あり"]  # * は何も知らせないので略す
    assert view["side_job"]["cells"] == [] and view["start"]["cells"] == []
    assert view["night_duty"]["cells"] and all(cell["kind"] == "accept" for cell in view["night_duty"]["cells"])
    assert [item["axis"] for item in worst_case_view(entries, ())] == list(AXIS_KEYS)


def test_the_rounded_cell_always_contains_the_stated_value_for_every_salary_in_the_range():
    # 丸めた後のマスは、本人が言った値を必ず含む(最悪でも、このマスの中のどこか、までしか知られない)
    for kind in ("accept", "reject"):
        for stated in range(300, 1501, 7):
            entry = _entry("e", kind, {**(ACCEPT_650 if kind == "accept" else REJECT_NIGHT_4), "salary": stated})
            salary = worst_case_view([entry], ())[0]
            if not salary["cells"]:  # 何も制約しない値(受けるの最低・受けないの最高)は略される
                continue
            cell = salary["cells"][0]
            low_ok = cell["low"] is None or stated > cell["low"] or (cell["low_inclusive"] and stated == cell["low"])
            high_ok = cell["high"] is None or stated < cell["high"] or (cell["high_inclusive"] and stated == cell["high"])
            assert low_ok and high_ok, (kind, stated, cell)
    assert rounded_anchor(_entry("x", "accept", {**ACCEPT_650, "salary": 620})).salary == 650


# ---------------------------------------------------------------------------
# 企業の一覧(§5 の 8)
# ---------------------------------------------------------------------------


def _write_case(directory, number, company_id, company_name, *, confidential=False):
    (directory / f"case{number}.toml").write_text(
        f"""case = {number}
[employer]
template_id = "t{number}"
company_id = "{company_id}"
job_id = "job{number}"
company_name = "{company_name}"
[employer.public_job]
title = "秘密の求人タイトル"
summary = "秘密の概要"
confidential = {str(confidential).lower()}
job_category = "it_web"
""",
        encoding="utf-8",
    )


def test_the_company_list_has_every_company_without_saying_whether_it_has_jobs(tmp_path):
    _write_case(tmp_path, 1, "company-b", "B 株式会社")
    _write_case(tmp_path, 2, "company-a", "A 株式会社", confidential=True)  # 非公開求人しかない企業も選べる
    _write_case(tmp_path, 3, "company-b", "B 株式会社(別の求人)")  # 同じ企業 ID は 1 件にまとめる
    (tmp_path / "interview_templates.toml").write_text("provisional = true", encoding="utf-8")  # case*.toml 以外は読まない
    companies = list_companies(tmp_path)
    assert companies == [
        {"company_id": "company-a", "company_name": "A 株式会社"},
        {"company_id": "company-b", "company_name": "B 株式会社"},
    ]
    assert all(set(company) == {"company_id", "company_name"} for company in companies)  # 求人の有無・件数・非公開かどうかは含まない
    assert "秘密" not in repr(companies)


def test_the_default_company_list_comes_from_the_fixtures():
    companies = list_companies()
    assert {"company_id": "case1-company", "company_name": "株式会社サンプルシステムズ(架空)"} in companies
    assert DEFAULT_WEB_CONFIG.limits.max_blocklist_entries >= len(companies)
