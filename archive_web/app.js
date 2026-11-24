"use strict";

const app = window.Telegram?.WebApp;
const byId = id => document.getElementById(id);
const dateFormat = new Intl.DateTimeFormat("ru", {dateStyle: "medium"});
const timeFormat = new Intl.DateTimeFormat("ru", {hour: "2-digit", minute: "2-digit"});
let chats = [];
let activeChat = null;
let deletedOnly = false;
let olderCursor = null;
let pageController;

function element(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = text ?? "";
  return node;
}

function status(id, text) {
  byId(id).textContent = text;
  byId(id).hidden = !text;
}

async function request(path, signal) {
  const response = await fetch("api/" + path, {
    headers: {"X-Telegram-Init-Data": app.initData},
    signal,
  });
  if (!response.ok) {
    const errors = {
      401: "Сеанс закончился. Закрой архив и открой его снова в CueMe.",
      403: "Архив доступен с активным CueMe Premium.",
      404: "Файл больше недоступен.",
      413: "Этот файл слишком большой для скачивания из архива.",
    };
    throw new Error(errors[response.status] || "Не получилось загрузить. Попробуй ещё раз.");
  }
  return response;
}

function renderChats() {
  const query = byId("search").value.trim().toLocaleLowerCase("ru");
  const visible = chats.filter(chat => chat.name.toLocaleLowerCase("ru").includes(query));
  const list = byId("chat-list");
  list.replaceChildren();
  for (const chat of visible) {
    const row = element("button", "chat-row");
    row.type = "button";
    const initials = chat.name.trim().split(/\s+/).slice(0, 2).map(word => Array.from(word)[0]).join("");
    const avatar = element("span", "avatar", initials.toLocaleUpperCase("ru"));
    avatar.setAttribute("aria-hidden", "true");
    const meta = element("span", "chat-meta");
    const date = new Date(chat.recent);
    meta.append(element("strong", "", chat.name));
    if (!Number.isNaN(date.valueOf())) meta.append(element("time", "", dateFormat.format(date)));
    const arrow = element("span", "arrow", "›");
    arrow.setAttribute("aria-hidden", "true");
    row.append(avatar, meta, arrow);
    row.addEventListener("click", () => openChat(chat));
    list.append(row);
  }
  status("inbox-status", visible.length ? "" : query ? "Ничего не нашлось." : "Здесь появятся сохранённые переписки.");
}

async function download(message, button) {
  button.disabled = true;
  try {
    const response = await request("files/" + message.id, AbortSignal.timeout(34000));
    const url = URL.createObjectURL(await response.blob());
    const filename = response.headers.get("Content-Disposition")?.match(/filename="([^"]+)"/)?.[1];
    const link = element("a", "");
    link.href = url;
    link.download = filename || "cueme-media";
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (error) {
    button.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function messageNode(message) {
  const node = element("div", "entry");
  const date = message.date ? new Date(message.date) : null;
  node.dataset.day = date ? dateFormat.format(date) : "Дата неизвестна";
  const day = element("p", "day", node.dataset.day);
  const bubble = element("article", message.out ? "bubble out" : "bubble");
  if (message.kind) bubble.append(element("p", "kind", [message.kind, message.details].filter(Boolean).join(" · ")));
  if (message.text) bubble.append(element("p", "", message.text));
  if (!message.text && !message.kind) bubble.append(element("p", "", "Содержимое не сохранилось"));
  if (message.attachment) {
    const button = element("button", "attachment", "Скачать вложение");
    button.type = "button";
    button.addEventListener("click", () => download(message, button));
    bubble.append(button);
  }
  const footer = element("footer", "");
  if (message.deleted) footer.append(element("strong", "", "Удалено"));
  if (date) footer.append(element("time", "", timeFormat.format(date)));
  bubble.append(footer);
  node.append(day, bubble);
  return node;
}

async function loadMessages(appendOlder = false) {
  pageController?.abort();
  pageController = new AbortController();
  const controller = pageController;
  const history = byId("history");
  const messages = byId("messages");
  const more = byId("more");
  if (!appendOlder) {
    messages.replaceChildren();
    olderCursor = null;
    more.hidden = true;
  }
  more.disabled = true;
  status("chat-status", "Загружаем сообщения…");
  try {
    const params = new URLSearchParams({deleted: deletedOnly ? "1" : "0"});
    if (olderCursor) params.set("before", JSON.stringify(olderCursor));
    const response = await request("chats/" + activeChat.id + "?" + params,
      AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]));
    const data = await response.json();
    if (controller.signal.aborted) return;
    olderCursor = data.next;
    const anchor = messages.firstElementChild;
    const previousTop = anchor?.getBoundingClientRect().top;
    const fragment = document.createDocumentFragment();
    for (const message of data.messages.reverse()) fragment.append(messageNode(message));
    messages.prepend(fragment);
    let previousDay;
    for (const node of messages.children) {
      node.firstElementChild.hidden = node.dataset.day === previousDay;
      previousDay = node.dataset.day;
    }
    more.hidden = !olderCursor;
    const empty = deletedOnly ? "Здесь будут сообщения, удаление которых CueMe зафиксировал." : "В этом чате пока нет сохранённых сообщений.";
    status("chat-status", messages.children.length ? "" : empty);
    if (anchor) history.scrollTop += anchor.getBoundingClientRect().top - previousTop;
    else history.scrollTop = history.scrollHeight;
  } catch (error) {
    if (!controller.signal.aborted) {
      more.hidden = false;
      more.textContent = "Попробовать ещё раз";
      status("chat-status", error.message);
    }
  } finally {
    if (!controller.signal.aborted) more.disabled = false;
  }
}

function setFilter(onlyDeleted) {
  deletedOnly = onlyDeleted;
  byId("all").setAttribute("aria-pressed", String(!deletedOnly));
  byId("deleted").setAttribute("aria-pressed", String(deletedOnly));
  byId("more").textContent = "Загрузить более ранние";
  void loadMessages();
}

function openChat(chat) {
  activeChat = chat;
  byId("chat-name").textContent = chat.name;
  byId("inbox").hidden = true;
  byId("conversation").hidden = false;
  app.BackButton?.show();
  setFilter(false);
}

function back() {
  pageController?.abort();
  byId("conversation").hidden = true;
  byId("inbox").hidden = false;
  app.BackButton?.hide();
}

async function start() {
  const started = performance.now();
  app?.ready();
  app?.expand();
  if (app?.isVersionAtLeast("6.1")) {
    app.setHeaderColor("#140f19");
    app.setBackgroundColor("#140f19");
  }
  app?.BackButton?.onClick(back);
  byId("search").addEventListener("input", renderChats);
  byId("back").addEventListener("click", back);
  byId("all").addEventListener("click", () => { if (deletedOnly) setFilter(false); });
  byId("deleted").addEventListener("click", () => { if (!deletedOnly) setFilter(true); });
  byId("more").addEventListener("click", () => {
    byId("more").textContent = "Загрузить более ранние";
    void loadMessages(Boolean(olderCursor));
  });
  try {
    if (!app?.initData) throw new Error("Открой архив из бота CueMe, в разделе «Удалённые сообщения».");
    const response = await request("chats", AbortSignal.timeout(15000));
    chats = (await response.json()).chats;
    byId("search-box").hidden = !chats.length;
    renderChats();
  } catch (error) {
    status("inbox-status", error.message);
  } finally {
    await new Promise(resolve => setTimeout(resolve, Math.max(0, 550 - (performance.now() - started))));
    byId("splash").hidden = true;
    byId("app").hidden = false;
  }
}

void start();
