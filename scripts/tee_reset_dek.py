"""debug イメージの VM で作った・復号した DEK を捨て、本番イメージの VM で作り直せるようにする(research/tee-spike.md 2-5・R22、契約 §13)。

    uv run python scripts/tee_reset_dek.py                                  # 何も削除せず、説明だけ出す(終了コード 1)
    uv run python scripts/tee_reset_dek.py --yes [--project <PROJECT_ID>]   # vault-db の _tee/dek と _tee/selftest を削除する

debug イメージの VM では、運営者が SSH で root として入れる。その間に金庫が作った DEK は「運営者が見られた鍵」なので、
本物の依頼者のデータを入れる前に、_tee/dek(KMS で包んだ DEK)と _tee/selftest(封印の自己試験の文書)を削除する。
金庫は、_tee/dek が無ければ、起動のときに新しい DEK を作る。だから、削除の後に金庫の VM を止めて開始すると、
その DEK は本番イメージの VM の中で作られる。
削除の前に、KEK の新しい版を primary にして古い版を無効化する(tests/manual/tee-spike.md の点 2)。これをしないと、運営者が見た DEK の包みを
書き戻される恐れが残る(金庫は、primary でない版で包まれた _tee/dek では起動しない)。

- 手元の ADC(gcloud auth application-default login)で、本物の Firestore の vault-db を操作する。--project を省くと、ADC の既定のプロジェクト。
  どこに対して削除するか(本物の Firestore かエミュレータか、プロジェクト)は、削除の前に表示する。
- --yes が無ければ、何も削除せず(Firestore にも接続せず)、説明だけを出して終了コード 1 で終わる(実行していないことが、終了コードで分かるように)。
- 古い DEK で封印した文書は、削除の後は開けなくなる。本物のデータを入れた後には実行しない。
- 文書の中身(包まれた DEK を含む)は、読んでも表示しない。

終了コード: 0 = 削除した(もともと無かった文書があっても 0)、1 = --yes が無い・接続や削除に失敗した。
"""

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from vault.firestore_client import VAULT_DATABASE, create_client  # noqa: E402

# 金庫(src/vault/tee/)が使う文書。dek は契約 §5 の DEK の保管、selftest は §4 の封印の自己試験。
DOCUMENT_PATHS = ("_tee/dek", "_tee/selftest")

EXPLANATION = f"""\
tee_reset_dek: 何も削除していません(--yes がありません)。

このスクリプトは、金庫のデータベース {VAULT_DATABASE} の、次の 2 文書を削除します。
  {DOCUMENT_PATHS[0]}       KMS で包んだ DEK(封印の鍵)
  {DOCUMENT_PATHS[1]}  封印の自己試験の文書

目的: debug イメージの VM の間に作った・復号した DEK は、運営者が SSH で root として見られた鍵です(research/tee-spike.md R22)。
本物の依頼者のデータを入れる前に捨てて、本番イメージの VM で作り直します。

進め方:
  0. 先に、KEK の新しい版を primary にして、古い版を無効化する(tests/manual/tee-spike.md の点 2)。
     これをしないと、運営者が見た DEK の包みを書き戻される恐れが残ります。
  1. 金庫の VM を止める(手順 F の stop)。
  2. uv run python scripts/tee_reset_dek.py --yes --project <PROJECT_ID>
  3. 金庫の VM を開始する(手順 F の start)。新しい DEK は、この起動で作られる。
  4. 起動のログに「sealing self-test ok」が出ることを確かめる。

注意: 古い DEK で封印した文書は、削除の後は開けなくなります。本物のデータを入れた後には実行しないでください。
接続先は、手元の ADC(gcloud auth application-default login)です。--project で対象のプロジェクトを指定できます(省略時は ADC の既定)。"""

NEXT_STEP = "次: 金庫の VM を止めて開始し(手順 F)、起動のログに「sealing self-test ok」が出ることを確かめる。新しい DEK は、その起動で作られる。"

ERROR_HINT = (
    "認証のエラーのときは、gcloud auth application-default login を実行する。"
    "quota project のエラーのときは、gcloud auth application-default set-quota-project <PROJECT_ID> を実行する。"
)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="debug イメージの間に作った DEK(vault-db の _tee/dek と _tee/selftest)を削除する。")
    parser.add_argument("--yes", action="store_true", help="削除を実行する。無ければ、説明だけを出して何もしない")
    parser.add_argument("--project", help="Firestore のプロジェクト ID(省略時は ADC の既定)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.yes:
        print(EXPLANATION)
        return 1

    try:
        client = create_client(project=args.project)
        try:
            emulator = os.environ.get("FIRESTORE_EMULATOR_HOST")
            target = f"エミュレータ({emulator})" if emulator else "本物の Firestore"
            print(f"接続先: {target} / プロジェクト {client.project} / データベース {VAULT_DATABASE}", flush=True)
            for path in DOCUMENT_PATHS:
                document = client.document(path)
                existed = document.get().exists
                document.delete()
                print(f"{'削除した' if existed else 'もともと無かった'}: {path}", flush=True)
        finally:
            client.close()
    except Exception as exc:
        print(f"エラー: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(ERROR_HINT, file=sys.stderr)
        return 1

    print(NEXT_STEP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
