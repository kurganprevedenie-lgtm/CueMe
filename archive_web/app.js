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
let viewedMedia = null;
const zoom = {scale: 1, x: 0, y: 0, pointers: new Map(), pinched: false, lastTap: null, lastTouchZoom: -Infinity};
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
  if (byId("media-viewer").open) byId("media-viewer").close();
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

function downloadMedia({url, filename}) {
  const link = element("a", "");
  link.href = url;
  link.download = filename || "cueme-media";
  document.body.append(link);
  link.click();
  link.remove();
}

function showViewerMenu(show) {
  byId("viewer-menu").hidden = !show;
  byId("viewer-options").setAttribute("aria-expanded", String(show));
}

function applyZoom(scale, x = zoom.x, y = zoom.y) {
  const stage = byId("viewer-stage");
  const image = stage.querySelector("img");
  if (!image) return;
  zoom.scale = Math.max(1, Math.min(4, scale));
  const limitX = Math.max(0, (image.offsetWidth * zoom.scale - stage.clientWidth) / 2);
  const limitY = Math.max(0, (image.offsetHeight * zoom.scale - stage.clientHeight) / 2);
  zoom.x = Math.max(-limitX, Math.min(limitX, x));
  zoom.y = Math.max(-limitY, Math.min(limitY, y));
  image.style.transform = `translate(${zoom.x}px, ${zoom.y}px) scale(${zoom.scale})`;
  byId("viewer-less").disabled = zoom.scale <= 1;
  byId("viewer-more").disabled = zoom.scale >= 4;
}

async function openMediaViewer(message, getFile) {
  const dialog = byId("media-viewer");
  const view = {getFile};
  viewedMedia = view;
  Object.assign(zoom, {scale: 1, x: 0, y: 0, pinched: false, lastTap: null, lastTouchZoom: -Infinity});
  zoom.pointers.clear();
  byId("viewer-title").textContent = message.kind || "Медиа";
  byId("viewer-stage").replaceChildren();
  byId("viewer-stage").classList.remove("is-photo");
  byId("viewer-zoom").hidden = true;
  byId("viewer-options").disabled = false;
  byId("viewer-download").disabled = false;
  showViewerMenu(false);
  status("viewer-status", "Загружаем…");
  dialog.showModal();
  try {
    const {url, mime} = await getFile();
    if (!dialog.open || viewedMedia !== view) return;
    const image = mime.startsWith("image/");
    if (!image && !mime.startsWith("video/")) throw new Error("Файл можно скачать через меню сверху.");
    const media = element(image ? "img" : "video", "viewer-content");
    if (image) {
      media.alt = message.kind || "Фото";
      media.draggable = false;
      byId("viewer-stage").classList.add("is-photo");
      byId("viewer-zoom").hidden = false;
    } else {
      media.controls = true;
      media.playsInline = true;
      media.preload = "metadata";
    }
    media.addEventListener("error", () => {
      if (viewedMedia === view) status("viewer-status", "Формат можно скачать через меню сверху.");
    }, {once: true});
    media.src = url;
    byId("viewer-stage").append(media);
    applyZoom(1, 0, 0);
    status("viewer-status", "");
  } catch (error) {
    if (viewedMedia === view) status("viewer-status", error.message || "Не получилось открыть файл.");
  }
}

function setupMediaViewer() {
  const dialog = byId("media-viewer");
  const stage = byId("viewer-stage");
  const menu = byId("viewer-menu");
  const options = byId("viewer-options");
  byId("viewer-close").addEventListener("click", () => dialog.close());
  options.addEventListener("click", () => showViewerMenu(menu.hidden));
  dialog.addEventListener("click", event => {
    if (!menu.contains(event.target) && !options.contains(event.target)) showViewerMenu(false);
  });
  dialog.addEventListener("close", () => {
    if (dialog.open) return;
    viewedMedia = null;
    zoom.pointers.clear();
    stage.replaceChildren();
  });
  byId("viewer-download").addEventListener("click", async () => {
    const view = viewedMedia;
    if (!view) return;
    const button = byId("viewer-download");
    button.disabled = true;
    try {
      const file = await view.getFile();
      if (viewedMedia !== view) return;
      downloadMedia(file);
      showViewerMenu(false);
      options.focus();
    } catch (error) {
      if (viewedMedia === view) status("viewer-status", error.message);
    } finally {
      if (viewedMedia === view) button.disabled = false;
    }
  });
  byId("viewer-more").addEventListener("click", () => applyZoom(zoom.scale + .5));
  byId("viewer-less").addEventListener("click", () => applyZoom(zoom.scale - .5));
  stage.addEventListener("dblclick", () => {
    if (performance.now() - zoom.lastTouchZoom > 350) applyZoom(zoom.scale > 1 ? 1 : 2, 0, 0);
  });
  stage.addEventListener("pointerdown", event => {
    if (!stage.classList.contains("is-photo")) return;
    if (!zoom.pointers.size) zoom.pinched = false;
    zoom.pinched ||= zoom.pointers.size > 0;
    zoom.pointers.set(event.pointerId, {x: event.clientX, y: event.clientY, startX: event.clientX, startY: event.clientY});
    stage.setPointerCapture(event.pointerId);
  });
  stage.addEventListener("pointermove", event => {
    const point = zoom.pointers.get(event.pointerId);
    if (!point) return;
    const other = [...zoom.pointers.entries()].find(([id]) => id !== event.pointerId)?.[1];
    const dx = event.clientX - point.x;
    const dy = event.clientY - point.y;
    if (other) {
      const before = Math.hypot(point.x - other.x, point.y - other.y);
      const after = Math.hypot(event.clientX - other.x, event.clientY - other.y);
      if (before > 0) applyZoom(zoom.scale * after / before, zoom.x + dx / 2, zoom.y + dy / 2);
    } else if (zoom.scale > 1) {
      applyZoom(zoom.scale, zoom.x + dx, zoom.y + dy);
    }
    point.x = event.clientX;
    point.y = event.clientY;
  });
  stage.addEventListener("pointerup", event => {
    const point = zoom.pointers.get(event.pointerId);
    zoom.pointers.delete(event.pointerId);
    if (event.pointerType !== "touch" || !point || zoom.pinched
        || Math.hypot(point.x - point.startX, point.y - point.startY) > 10) return;
    const now = performance.now();
    const last = zoom.lastTap;
    if (last && now - last.time < 300 && Math.hypot(last.x - point.x, last.y - point.y) < 35) {
      applyZoom(zoom.scale > 1 ? 1 : 2, 0, 0);
      zoom.lastTouchZoom = now;
      zoom.lastTap = null;
    } else {
      zoom.lastTap = {time: now, x: point.x, y: point.y};
    }
  });
  stage.addEventListener("pointercancel", event => zoom.pointers.delete(event.pointerId));
  addEventListener("resize", () => { if (dialog.open) applyZoom(zoom.scale); });
}

function attachmentNode(message) {
  const box = element("div", "media");
  const scope = mediaSession.signal;
  const feedback = element("span", "media-status");
  feedback.setAttribute("role", "status");
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
  const visual = message.preview === "image" || message.preview === "video";
  if (!visual) {
    const save = element("button", "attachment", "Скачать");
    save.type = "button";
    save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        downloadMedia(await getFile());
        feedback.textContent = "";
      } catch (error) {
        if (!scope.aborted) feedback.textContent = error.message;
      } finally {
        save.disabled = false;
      }
    });
    box.append(save);
  }
  if (visual) {
    const isPhoto = message.preview === "image";
    const open = element("button", "media-open media-tile", isPhoto ? "Открыть фото" : "▶ Смотреть видео");
    open.type = "button";
    open.addEventListener("click", () => void openMediaViewer(message, getFile));
    const preview = element("span", "media-tile-thumb");
    open.prepend(preview);
    box.prepend(open);
    if (isPhoto && photoObserver) {
      previewLoaders.set(box, async () => {
        try {
          const {url, mime} = await getFile();
          if (!mime.startsWith("image/")) return;
          const image = element("img", "media-preview");
          image.alt = "";
          image.src = url;
          await image.decode();
          open.setAttribute("aria-label", "Открыть фото");
          open.classList.add("photo-ready");
          open.replaceChildren(image);
        } catch {
          // Кнопка просмотра позволяет повторить запрос.
        }
      });
      photoObserver.observe(box);
    } else if (message.thumbnail) {
      observeListImage(preview, "thumbs/" + message.id);
    }
  } else if (message.preview === "audio") {
    const label = "Загрузить " + (message.kind || "аудио").toLowerCase();
    const open = element("button", "media-open", label);
    open.type = "button";
    const load = async () => {
      if (scope.aborted || open.disabled) return;
      open.disabled = true;
      open.textContent = "Загружаем…";
      try {
        const {url, mime} = await getFile();
        const family = mime.split("/")[0];
        if (family !== "audio") throw new Error("Для этого формата доступно скачивание.");
        const player = element("audio", "media-preview");
        player.controls = true;
        player.preload = "metadata";
        player.setAttribute("aria-label", message.kind || "Аудио");
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
    box.prepend(open);
  }
  box.append(feedback);
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
  setupMediaViewer();
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
