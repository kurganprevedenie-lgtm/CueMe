"use strict";

const app = window.Telegram?.WebApp;
const byId = id => document.getElementById(id);
const dateFormat = new Intl.DateTimeFormat("ru", {dateStyle: "medium"});
const timeFormat = new Intl.DateTimeFormat("ru", {hour: "2-digit", minute: "2-digit"});
const shortDate = new Intl.DateTimeFormat("ru", {day: "numeric", month: "short"});
let chats = [];
let activeChat = null;
let deletedOnly = false;
let olderCursor = null;
let pageController;
let mediaSession = new AbortController();
const mediaUrls = new Set();
const listImages = new Map();
const listImageObserver = typeof IntersectionObserver === "function" ? new IntersectionObserver(entries => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    listImageObserver.unobserve(entry.target);
    void showListImage(entry.target);
  }
}, {rootMargin: "70px 0px"}) : null;
const previewLoaders = new WeakMap();
const photoObserver = typeof IntersectionObserver === "function" ? new IntersectionObserver(entries => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    photoObserver.unobserve(entry.target);
    previewLoaders.get(entry.target)?.();
  }
}, {root: byId("history"), rootMargin: "120px 0px"}) : null;

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

function initials(name) {
  return name.trim().split(/\s+/).slice(0, 2).map(word => Array.from(word)[0]).join("").toLocaleUpperCase("ru") || "•";
}

function renderAvatar(node, chat) {
  const tone = Array.from(chat.id).reduce((sum, character) => sum * 31 + character.codePointAt(0) | 0, 0);
  node.className = "avatar avatar-tone-" + (Math.abs(tone) % 6);
  node.textContent = initials(chat.name);
  if (chat.avatar) observeListImage(node, "avatars/" + chat.id);
}

function observeListImage(node, path) {
  node.dataset.imagePath = path;
  if (listImageObserver) listImageObserver.observe(node);
  else void showListImage(node);
}

async function showListImage(node) {
  const path = node.dataset.imagePath;
  if (!node.isConnected) return;
  if (!listImages.has(path)) {
    listImages.set(path, request(path, AbortSignal.timeout(10000))
      .then(response => response.blob())
      .then(blob => URL.createObjectURL(blob))
      .catch(() => null));
    if (listImages.size > 96) {
      const oldest = listImages.keys().next().value;
      listImages.get(oldest).then(url => { if (url) URL.revokeObjectURL(url); });
      listImages.delete(oldest);
    }
  }
  const url = await listImages.get(path);
  if (!node.isConnected || node.dataset.imagePath !== path) return;
  if (!url) {
    delete node.dataset.imagePath;
    return;
  }
  const image = element("img", "");
  image.alt = "";
  image.src = url;
  try {
    await image.decode();
    if (node.isConnected && node.dataset.imagePath === path) {
      node.append(image);
      node.classList.add("image-ready");
    }
  } catch {
    // Для недоступного фото остаются инициалы или тип медиа.
    if (node.dataset.imagePath === path) delete node.dataset.imagePath;
  }
}

function resetMedia() {
  photoObserver?.disconnect();
  mediaSession.abort();
  mediaSession = new AbortController();
  for (const url of mediaUrls) URL.revokeObjectURL(url);
  mediaUrls.clear();
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
  const now = new Date();
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  for (const node of list.querySelectorAll("[data-image-path]")) listImageObserver?.unobserve(node);
  list.replaceChildren();
  for (const chat of visible) {
    const row = element("button", "chat-row");
    row.type = "button";
    const avatar = element("span", "avatar");
    avatar.setAttribute("aria-hidden", "true");
    const meta = element("span", "chat-meta");
    const top = element("span", "chat-row-top");
    const date = new Date(chat.recent);
    top.append(element("strong", "", chat.name));
    if (!Number.isNaN(date.valueOf())) {
      const label = date.toDateString() === now.toDateString() ? timeFormat.format(date)
        : date.toDateString() === yesterday.toDateString() ? "Вчера" : shortDate.format(date);
      const timestamp = element("time", "", label);
      timestamp.dateTime = chat.recent;
      top.append(timestamp);
    }
    meta.append(top);
    const previewLine = element("span", "chat-preview-line");
    const thumbnail = element("span", "chat-thumbnail");
    thumbnail.setAttribute("aria-hidden", "true");
    const previewText = element("span", "chat-preview");
    if (chat.out) previewText.append(element("span", "chat-sender", "Вы: "));
    if (chat.kind && chat.kind !== chat.preview) previewText.append(element("span", "chat-sender", chat.kind + " · "));
    previewText.append(document.createTextNode(chat.preview || chat.kind || ""));
    previewLine.append(thumbnail, previewText);
    if (chat.preview || chat.kind) meta.append(previewLine);
    row.append(avatar, meta);
    row.addEventListener("click", () => openChat(chat));
    list.append(row);
    renderAvatar(avatar, chat);
    if (chat.thumbnail) observeListImage(thumbnail, "thumbs/" + chat.thumbnail);
  }
  status("inbox-status", visible.length ? "" : query ? "Ничего не нашлось." : "Здесь появятся сохранённые переписки.");
}

function attachmentNode(message) {
  const box = element("div", "media");
  const scope = mediaSession.signal;
  const feedback = element("span", "media-status");
  feedback.setAttribute("role", "status");
  const save = element("button", "attachment", "Скачать");
  save.type = "button";
  let file, pending;
  async function getFile() {
    if (file) return file;
    if (pending) return pending;
    pending = (async () => {
      const response = await request("files/" + message.id,
        AbortSignal.any([scope, AbortSignal.timeout(34000)]));
      const blob = await response.blob();
      if (scope.aborted) throw scope.reason;
      const url = URL.createObjectURL(blob);
      mediaUrls.add(url);
      const filename = response.headers.get("Content-Disposition")?.match(/filename="([^"]+)"/)?.[1];
      file = {url, filename, mime: blob.type};
      return file;
    })();
    try {
      return await pending;
    } finally {
      pending = null;
    }
  }
  save.addEventListener("click", async () => {
    save.disabled = true;
    try {
      const {url, filename} = await getFile();
      const link = element("a", "");
      link.href = url;
      link.download = filename || "cueme-media";
      document.body.append(link);
      link.click();
      link.remove();
      feedback.textContent = "";
    } catch (error) {
      if (!scope.aborted) feedback.textContent = error.message;
    } finally {
      save.disabled = false;
    }
  });
  if (message.preview) {
    const label = message.preview === "image" ? "Показать" : "Загрузить " + (message.kind || "медиа").toLowerCase();
    const open = element("button", "media-open", label);
    open.type = "button";
    const load = async () => {
      if (scope.aborted || open.disabled) return;
      open.disabled = true;
      open.textContent = "Загружаем…";
      try {
        const {url, mime} = await getFile();
        const family = mime.split("/")[0];
        if (!["image", "video", "audio"].includes(family)) throw new Error("Для этого формата доступно скачивание.");
        const player = element(family === "image" ? "img" : family, "media-preview");
        if (family === "image") {
          player.alt = message.kind || "Изображение";
        } else {
          player.controls = true;
          player.preload = "metadata";
          player.playsInline = true;
          player.setAttribute("aria-label", message.kind || "Медиа");
          if (message.round) player.classList.add("round-video");
        }
        player.addEventListener("error", () => {
          player.remove();
          feedback.textContent = "Формат не поддерживается на этом устройстве. Попробуй скачать файл.";
        }, {once: true});
        player.src = url;
        open.replaceWith(player);
      } catch (error) {
        if (!scope.aborted) feedback.textContent = error.message;
        open.textContent = label;
        open.disabled = false;
      }
    };
    open.addEventListener("click", load);
    box.append(open);
    if (message.preview === "image" && photoObserver) {
      previewLoaders.set(box, load);
      photoObserver.observe(box);
    }
  }
  box.append(save, feedback);
  return box;
}

function messageNode(message) {
  const node = element("div", "entry");
  const date = message.date ? new Date(message.date) : null;
  node.dataset.day = date ? dateFormat.format(date) : "Дата неизвестна";
  const day = element("p", "day", node.dataset.day);
  const bubble = element("article", message.out ? "bubble out" : "bubble");
  if (message.kind) bubble.append(element("p", "kind", [message.kind, message.details].filter(Boolean).join(" · ")));
  if (message.attachment) bubble.append(attachmentNode(message));
  if (message.text) bubble.append(element("p", "", message.text));
  if (!message.text && !message.kind) bubble.append(element("p", "", "Содержимое не сохранилось"));
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
    resetMedia();
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
  const avatar = byId("chat-avatar");
  delete avatar.dataset.imagePath;
  byId("inbox").hidden = true;
  byId("conversation").hidden = false;
  renderAvatar(avatar, chat);
  byId("conversation").classList.remove("condensed");
  setFilter(false);
}

function back() {
  pageController?.abort();
  resetMedia();
  byId("messages").replaceChildren();
  byId("conversation").hidden = true;
  byId("inbox").hidden = false;
}

async function start() {
  const started = performance.now();
  byId("archive-logo").src = byId("splash-logo").src;
  const wallpaper = new Image();
  wallpaper.src = document.querySelector('link[rel="preload"][as="image"]').href;
  const wallpaperReady = Promise.race([
    wallpaper.decode().catch(() => {}),
    new Promise(resolve => setTimeout(resolve, 1100)),
  ]);
  app?.ready();
  app?.expand();
  if (app?.isVersionAtLeast("6.1")) {
    app.setHeaderColor("#0d0912");
    app.setBackgroundColor("#0d0912");
  }
  byId("history").addEventListener("scroll", event => {
    const history = event.currentTarget;
    const chat = byId("conversation");
    const distance = history.scrollHeight - history.clientHeight - history.scrollTop;
    if (distance > 116) chat.classList.add("condensed");
    else if (distance < 30) chat.classList.remove("condensed");
  }, {passive: true});
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
    await Promise.all([
      wallpaperReady,
      new Promise(resolve => setTimeout(resolve, Math.max(0, 550 - (performance.now() - started)))),
    ]);
    byId("splash").hidden = true;
    byId("app").hidden = false;
  }
}

void start();
