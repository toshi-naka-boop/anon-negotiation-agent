"""テストごとの Firestore の分け方(tests/conftest.py。台帳 I-5)。

テストごとに、一意なプロジェクト ID(demo-test-{uuid})の Firestore クライアントを使う。エミュレータはプロジェクトごとに
データを分けるので、テストの後始末でデータを消さなくても、前のテストが残したデータ(止まらない別スレッドの呼び出しが、遅れて
書いたものを含む)は、次のテストに現れない。vault-db と (default) は、同じテストの中では同じプロジェクトを使う。
"""

import pytest

_seen_project_ids: set[str] = set()


@pytest.mark.parametrize("attempt", [1, 2, 3])
def test_each_test_has_its_own_emulator_project_and_starts_with_empty_databases(
    firestore_project_id, firestore_client, default_db, attempt
):
    # このテストのプロジェクト ID は、エミュレータ専用の名前(demo- で始まる)で、ほかのテストと重ならない。
    assert firestore_project_id.startswith("demo-test-")
    assert firestore_project_id not in _seen_project_ids
    _seen_project_ids.add(firestore_project_id)
    # vault-db と (default) は同じプロジェクトを使い、始まりは、どちらも空(前のテストのデータがない)。
    assert firestore_client.project == default_db.project == firestore_project_id
    assert next(iter(firestore_client.collections()), None) is None
    assert next(iter(default_db.collections()), None) is None

    # 次のテストに残るはずのデータを書く(後始末をしなくても、次のテストの「空」の確認が通ることを見る)。
    firestore_client.collection("left_behind").document("x").set({"attempt": attempt})
    default_db.collection("left_behind").document("x").set({"attempt": attempt})
    assert firestore_client.collection("left_behind").document("x").get().exists
