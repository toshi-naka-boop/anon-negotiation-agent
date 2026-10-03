/* 入口のページ。面談の入口の注記(面談 API の begin が返す notice と同じ中身)を出す。
 * begin は面談の状態をサーバーのメモリに作るので、ここでは呼ばず、読み取り専用の口から取る。 */

import { call } from "./api.js";
import { h, replace, showError } from "./ui.js";

async function showNotice() {
  const title = document.getElementById("notice-title");
  const body = document.getElementById("notice-body");
  try {
    const notice = await call("GET /v1/interview/notice");
    title.textContent = notice.title;
    replace(body, h("ul", {}, notice.items.map((item) => h("li", { dataset: { id: item.id } }, item.text))));
  } catch (error) {
    showError(body, error);
  }
}

showNotice();
