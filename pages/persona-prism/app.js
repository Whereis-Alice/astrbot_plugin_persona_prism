/**
 * 人格棱镜 · Persona Prism — Dashboard 前端
 *
 * 结构：桥接封装 -> 状态仓库 -> 通用 DOM 工具 -> 四个视图渲染器 -> 引导。
 * 所有节点都用 createElement 构建，后端返回的字符串一律走 textContent，
 * 绝不拼进 innerHTML，避免把画像文本当 HTML 解析。
 */

const bridge = window.AstrBotPluginPage;

const SVG_NS = "http://www.w3.org/2000/svg";
const THEME_KEY = "persona-prism-theme";
const PAGE_SIZE = 20;

const THEME_OPTIONS = [
  {
    value: "auto",
    label: "跟随系统",
    desc: "跟随浏览器明暗偏好自动切换",
    swatch: "linear-gradient(135deg, #0a0d16 0 48%, #f4f6fc 48% 100%)",
  },
  {
    value: "nocturne",
    label: "夜曜 Nocturne",
    desc: "深靛玻璃质感，久看不累",
    swatch: "linear-gradient(135deg, #0a0d16, #7c6cff 70%, #37d3e8)",
  },
  {
    value: "daylight",
    label: "晴昼 Daylight",
    desc: "明亮清爽，适合白天与投屏",
    swatch: "linear-gradient(135deg, #ffffff, #5b53f0 75%, #0a9fb8)",
  },
  {
    value: "neon",
    label: "霓境 Neon",
    desc: "高对比赛博霓虹，信息密度优先",
    swatch: "linear-gradient(135deg, #05060f, #ff2e9c 55%, #00f0ff)",
  },
  {
    value: "ink",
    label: "墨宣 Ink",
    desc: "宣纸衬线排版，安静阅读长文",
    swatch: "linear-gradient(135deg, #f3efe4, #8c2f22 78%, #3f5f4a)",
  },
];

const VIEWS = [
  { id: "overview", label: "概览", icon: "i-overview", kicker: "OVERVIEW / 运行状态" },
  { id: "records", label: "画像记录", icon: "i-records", kicker: "RECORDS / 按群与用户归档" },
  { id: "settings", label: "运行设置", icon: "i-sliders", kicker: "SETTINGS / 配置与隐私" },
  { id: "prompts", label: "提示词", icon: "i-prompt", kicker: "PROMPTS / 模板管理" },
];

const KIND_FILTERS = [
  { value: "", label: "全部类型" },
  { value: "portrait", label: "人格画像" },
  { value: "praise", label: "群友赞赏" },
  { value: "roast", label: "群友锐评" },
  { value: "clone", label: "人格克隆" },
  { value: "match", label: "群友姻缘" },
];

const state = {
  view: "overview",
  theme: "auto",
  booted: false,
  overview: null,
  tree: null,
  records: { items: [], page: 1, pages: 1, total: 0 },
  filters: { groupId: "", userId: "", kind: "", q: "" },
  expanded: new Set(),
  settings: null,
  draft: {},
  prompts: null,
  promptDraft: null,
  detail: null,
  busy: false,
};

const dom = {};

/* ------------------------------------------------------------------ 工具 */

function make(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) {
    node.className = cls;
  }
  if (text !== undefined && text !== null && text !== "") {
    node.textContent = String(text);
  }
  return node;
}

function icon(name, size) {
  const svg = document.createElementNS(SVG_NS, "svg");
  const px = String(size || 16);
  svg.setAttribute("class", "icon");
  svg.setAttribute("width", px);
  svg.setAttribute("height", px);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG_NS, "use");
  use.setAttribute("href", "#" + name);
  svg.appendChild(use);
  return svg;
}

function clear(node) {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
  return node;
}

function append(parent, children) {
  for (const child of children) {
    if (child) {
      parent.appendChild(child);
    }
  }
  return parent;
}

function fmtNum(value) {
  const num = Number(value || 0);
  if (!Number.isFinite(num)) {
    return "0";
  }
  return num.toLocaleString("zh-CN");
}

function fmtTime(ts, withTime) {
  const seconds = Number(ts || 0);
  if (!seconds) {
    return "—";
  }
  const date = new Date(seconds * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  const day = date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate());
  if (withTime === false) {
    return day;
  }
  return day + " " + pad(date.getHours()) + ":" + pad(date.getMinutes());
}

function fmtAgo(ts) {
  const seconds = Number(ts || 0);
  if (!seconds) {
    return "—";
  }
  const diff = Math.max(0, Math.floor(Date.now() / 1000) - seconds);
  if (diff < 60) {
    return "刚刚";
  }
  if (diff < 3600) {
    return Math.floor(diff / 60) + " 分钟前";
  }
  if (diff < 86400) {
    return Math.floor(diff / 3600) + " 小时前";
  }
  if (diff < 86400 * 30) {
    return Math.floor(diff / 86400) + " 天前";
  }
  return fmtTime(seconds, false);
}

function fmtBytes(value) {
  const num = Number(value || 0);
  if (num <= 0) {
    return "—";
  }
  if (num < 1024) {
    return num + " B";
  }
  if (num < 1024 * 1024) {
    return (num / 1024).toFixed(1) + " KB";
  }
  return (num / 1024 / 1024).toFixed(2) + " MB";
}

function pct(value) {
  const num = Number(value || 0);
  return Math.round(Math.max(0, Math.min(1, num)) * 100);
}

function confidenceTone(value) {
  const num = Number(value || 0);
  if (num >= 0.75) {
    return "pill--ok";
  }
  if (num >= 0.45) {
    return "pill--warn";
  }
  return "pill--danger";
}

function confidenceText(value) {
  const num = Number(value || 0);
  if (num >= 0.75) {
    return "证据充分";
  }
  if (num >= 0.45) {
    return "参考为主";
  }
  return "样本偏少";
}

/* ------------------------------------------------------------ 主题与提示条 */

function resolveTheme(mode) {
  if (mode && mode !== "auto") {
    return mode;
  }
  const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  return dark ? "nocturne" : "daylight";
}

function applyTheme(mode, persist) {
  state.theme = mode;
  document.documentElement.dataset.theme = resolveTheme(mode);
  if (persist) {
    try {
      window.localStorage.setItem(THEME_KEY, mode);
    } catch (err) {
      /* 隐私模式下 localStorage 可能不可写，忽略即可 */
    }
  }
  const option = THEME_OPTIONS.find((item) => item.value === mode) || THEME_OPTIONS[0];
  if (dom.themeLabel) {
    dom.themeLabel.textContent = option.label.split(" ")[0];
  }
  renderThemeMenu();
}

function renderThemeMenu() {
  if (!dom.themeMenu) {
    return;
  }
  clear(dom.themeMenu);
  for (const option of THEME_OPTIONS) {
    const btn = make("button", "themeopt");
    btn.type = "button";
    btn.setAttribute("aria-current", option.value === state.theme ? "true" : "false");
    const swatch = make("span", "themeopt__swatch");
    swatch.style.background = option.swatch;
    const text = make("span", "themeopt__text");
    append(text, [make("strong", "", option.label), make("small", "", option.desc)]);
    append(btn, [swatch, text, icon("i-check", 16)]);
    btn.addEventListener("click", () => {
      applyTheme(option.value, true);
      toggleThemeMenu(false);
    });
    dom.themeMenu.appendChild(btn);
  }
}

function toggleThemeMenu(force) {
  const open = force === undefined ? dom.themeMenu.hidden : force;
  dom.themeMenu.hidden = !open;
  dom.themeToggle.setAttribute("aria-expanded", open ? "true" : "false");
}

function toast(message, tone) {
  const kind = tone || "info";
  const node = make("div", "toast toast--" + kind);
  const symbol = kind === "ok" ? "i-check" : kind === "err" ? "i-alert" : "i-spark";
  append(node, [icon(symbol, 16), make("span", "", message)]);
  dom.toasts.appendChild(node);
  window.setTimeout(() => {
    node.remove();
  }, kind === "err" ? 6000 : 3200);
}

/* -------------------------------------------------------------- 桥接封装 */

function unwrap(payload) {
  if (!payload || payload.ok !== true) {
    const reason = payload && payload.error ? payload.error : "请求未返回可用数据。";
    throw new Error(reason);
  }
  return payload;
}

async function apiGet(endpoint, params) {
  if (!bridge) {
    throw new Error("AstrBot 页面桥接未加载。");
  }
  return unwrap(await bridge.apiGet(endpoint, params || {}));
}

async function apiPost(endpoint, body) {
  if (!bridge) {
    throw new Error("AstrBot 页面桥接未加载。");
  }
  return unwrap(await bridge.apiPost(endpoint, body || {}));
}

/* -------------------------------------------------------------- 通用零件 */

function pill(text, tone) {
  return make("span", tone ? "pill " + tone : "pill", text);
}

function statCard(label, value, note, symbol) {
  const card = make("article", "stat");
  const head = make("p", "stat__label");
  append(head, [icon(symbol || "i-spark", 14), make("span", "", label)]);
  append(card, [head, make("p", "stat__value", value), note ? make("p", "stat__note", note) : null]);
  return card;
}

function panel(title, subtitle, symbol, tools) {
  const box = make("section", "panel");
  const head = make("header", "panel__head");
  const text = make("div", "panel__head-text");
  append(text, [make("h2", "", title), subtitle ? make("p", "", subtitle) : null]);
  append(head, [icon(symbol || "i-spark", 18), text]);
  if (tools && tools.length) {
    const bar = make("div", "panel__tools");
    append(bar, tools);
    head.appendChild(bar);
  }
  const body = make("div", "panel__body");
  append(box, [head, body]);
  box.body = body;
  return box;
}

function kvRow(key, value) {
  const row = make("div", "kv__row");
  append(row, [make("span", "kv__key", key), make("span", "kv__val", value)]);
  return row;
}

function emptyState(title, detail, symbol) {
  const box = make("div", "empty");
  append(box, [
    icon(symbol || "i-database", 30),
    make("strong", "", title),
    detail ? make("p", "", detail) : null,
  ]);
  return box;
}

function loadingState(text) {
  const box = make("div", "loading");
  append(box, [make("span", "spinner"), make("span", "", text || "正在读取…")]);
  return box;
}

function notice(text, tone, symbol) {
  const box = make("p", tone ? "notice " + tone : "notice");
  append(box, [icon(symbol || "i-alert", 15), make("span", "", text)]);
  return box;
}

function button(label, opts) {
  const cfg = opts || {};
  const btn = make("button", "btn" + (cfg.variant ? " btn--" + cfg.variant : "") + (cfg.small ? " btn--sm" : ""));
  btn.type = "button";
  if (cfg.icon) {
    btn.appendChild(icon(cfg.icon, cfg.small ? 14 : 16));
  }
  if (label) {
    btn.appendChild(make("span", "", label));
  }
  if (cfg.title) {
    btn.title = cfg.title;
  }
  if (cfg.onClick) {
    btn.addEventListener("click", cfg.onClick);
  }
  return btn;
}

function barRow(label, value, total, color) {
  const row = make("div", "bar");
  const track = make("div", "bar__track");
  const fill = make("div", "bar__fill");
  const ratio = total > 0 ? Math.max(2, Math.round((value / total) * 100)) : 0;
  fill.style.width = ratio + "%";
  fill.style.background = color || "var(--chart-1)";
  track.appendChild(fill);
  append(row, [make("span", "", label), track, make("span", "bar__num", fmtNum(value))]);
  return row;
}

function meter(label, valueText, ratio) {
  const box = make("div", "meter");
  const head = make("div", "meter__head");
  append(head, [make("span", "", label), make("span", "", valueText)]);
  const track = make("div", "meter__track");
  const fill = make("div", "meter__fill");
  fill.style.width = pct(ratio) + "%";
  track.appendChild(fill);
  append(box, [head, track]);
  return box;
}

/* ---------------------------------------------------------------- 概览视图 */

function kindLabel(kind) {
  const hit = KIND_FILTERS.find((item) => item.value === kind);
  if (hit) {
    return hit.label;
  }
  if (state.prompts) {
    const custom = (state.prompts.custom || []).find((item) => item.key === kind);
    if (custom) {
      return custom.label || custom.key;
    }
  }
  return kind || "未知";
}

function renderOverview(root) {
  const data = state.overview;
  if (!data) {
    root.appendChild(loadingState("正在读取运行状态…"));
    return;
  }
  const stats = data.stats || {};
  const corpus = stats.corpus || {};
  const render = data.render || {};
  const flags = data.flags || {};

  const grid = make("div", "statgrid");
  append(grid, [
    statCard("画像总数", fmtNum(stats.portraits), "累计生成的画像记录", "i-records"),
    statCard("覆盖群聊", fmtNum(stats.groups), "至少生成过一次画像", "i-users"),
    statCard("覆盖群友", fmtNum(stats.users), "被画过像的不同用户", "i-user"),
    statCard("今日生成", fmtNum(stats.today), "从今天零点起算", "i-clock"),
    statCard("本地语料", fmtNum(corpus.total), fmtNum(corpus.users) + " 位群友的发言样本", "i-database"),
    statCard("自助隐身", fmtNum(stats.optouts), "主动退出画像的成员", "i-eye-off"),
  ]);
  root.appendChild(grid);

  const health = panel("运行健康度", "最近 7 天的任务执行情况", "i-gauge");
  const rate = Number(stats.success_rate || 0);
  append(health.body, [
    meter("成功率", pct(rate) + "%（近 7 天 " + fmtNum(stats.runs_7d) + " 次任务）", rate),
    (() => {
      const kv = make("div", "kv");
      append(kv, [
        kvRow("平均耗时", stats.avg_elapsed_ms ? (Number(stats.avg_elapsed_ms) / 1000).toFixed(1) + " 秒" : "—"),
        kvRow("提示词模板", fmtNum((data.prompts || {}).total) + " 套"),
        kvRow("语料最早", fmtTime(corpus.oldest, false)),
        kvRow("语料最新", fmtAgo(corpus.newest)),
        kvRow("插件版本", data.version || "—"),
        kvRow("服务器时间", fmtTime(data.server_time)),
      ]);
      return kv;
    })(),
  ]);
  if (rate > 0 && rate < 0.6 && Number(stats.runs_7d || 0) >= 5) {
    health.body.appendChild(
      notice("成功率偏低，多数失败集中在模型返回格式或超时。可到「运行设置」提高失败重试次数，或换一个更稳的服务提供商。", "notice--danger"),
    );
  }
  root.appendChild(health);

  const kinds = (stats.kinds || []).filter((item) => Number(item.total || 0) > 0);
  const dist = panel("画像类型分布", "各套模板被使用的次数", "i-layers");
  if (!kinds.length) {
    dist.body.appendChild(emptyState("还没有画像记录", "在群里发一条「棱镜画像」试试，生成的结果会自动出现在这里。", "i-prism"));
  } else {
    const max = Math.max(...kinds.map((item) => Number(item.total || 0)));
    const bars = make("div", "bars");
    kinds.forEach((item, index) => {
      bars.appendChild(
        barRow(kindLabel(item.kind), Number(item.total || 0), max, "var(--chart-" + ((index % 5) + 1) + ")"),
      );
    });
    dist.body.appendChild(bars);
  }
  root.appendChild(dist);

  const rendering = panel("渲染与开关", "卡片怎么出图、哪些能力已启用", "i-image");
  const kv = make("div", "kv");
  const labels = render.backend_labels || {};
  append(kv, [
    kvRow("渲染链路", render.backend_label || render.backend || "—"),
    kvRow("默认卡片主题", render.theme || "—"),
    kvRow("上次实际出图", labels[render.last_backend] || render.last_backend || "尚未渲染"),
  ]);
  rendering.body.appendChild(kv);

  const flagList = make("div", "taglist");
  const flagSpec = [
    ["passive_capture", "被动采集群聊"],
    ["inject_enabled", "画像注入对话"],
    ["allow_opt_out", "允许自助隐身"],
    ["redact_pii", "入库前脱敏"],
    ["clone_enabled", "人格克隆"],
    ["sync_bot_nickname", "同步机器人昵称"],
    ["sync_bot_avatar", "同步机器人头像"],
  ];
  for (const [key, label] of flagSpec) {
    const on = Boolean(flags[key]);
    flagList.appendChild(pill(label + "：" + (on ? "开" : "关"), on ? "pill--accent" : "pill--muted"));
  }
  rendering.body.appendChild(flagList);
  if (flags.sync_bot_nickname || flags.sync_bot_avatar) {
    rendering.body.appendChild(
      notice("机器人身份同步已开启：执行「棱镜克隆」时会真的改动机器人的全局昵称或头像，且无法自动恢复。如非必要建议在 AstrBot 插件配置页关掉。", "notice--danger"),
    );
  }
  root.appendChild(rendering);

  const runs = data.runs || [];
  const runPanel = panel("最近任务", "含失败原因，便于排查", "i-clock");
  runPanel.body.className = "panel__body panel__body--flush";
  if (!runs.length) {
    runPanel.body.appendChild(emptyState("暂无任务记录", "插件启动后每次画像都会在这里留一行日志。", "i-clock"));
  } else {
    const wrap = make("div", "tablewrap");
    const table = make("table", "table");
    const thead = make("thead");
    const hr = make("tr");
    for (const label of ["时间", "群", "目标", "类型", "结果", "出图", "耗时", "备注"]) {
      hr.appendChild(make("th", "", label));
    }
    thead.appendChild(hr);
    const tbody = make("tbody");
    for (const run of runs) {
      const tr = make("tr");
      tr.appendChild(make("td", "", fmtTime(run.created_at)));
      tr.appendChild(make("td", "", run.group_id || "私聊"));
      tr.appendChild(make("td", "", run.user_id || "—"));
      tr.appendChild(make("td", "", kindLabel(run.kind)));
      const resultCell = make("td");
      resultCell.appendChild(pill(run.ok ? "成功" : "失败", run.ok ? "pill--ok" : "pill--danger"));
      tr.appendChild(resultCell);
      tr.appendChild(make("td", "", labels[run.backend] || run.backend || "—"));
      tr.appendChild(make("td", "num", run.elapsed_ms ? (Number(run.elapsed_ms) / 1000).toFixed(1) + "s" : "—"));
      tr.appendChild(make("td", "wrap", run.error || ""));
      tbody.appendChild(tr);
    }
    append(table, [thead, tbody]);
    wrap.appendChild(table);
    runPanel.body.appendChild(wrap);
  }
  root.appendChild(runPanel);
}

/* -------------------------------------------------------------- 记录视图 */

function scopeLabel() {
  const f = state.filters;
  if (f.userId) {
    const group = (state.tree ? state.tree.groups || [] : []).find((g) => g.group_id === f.groupId);
    const member = group ? (group.members || []).find((m) => m.user_id === f.userId) : null;
    const who = member ? member.user_name : f.userId;
    return (group ? group.group_name + " · " : "") + who;
  }
  if (f.groupId) {
    const group = (state.tree ? state.tree.groups || [] : []).find((g) => g.group_id === f.groupId);
    return group ? group.group_name : "群 " + f.groupId;
  }
  return "全部记录";
}

function renderTree() {
  const box = panel("群聊目录", "先选群，再选群友", "i-users");
  box.body.className = "panel__body panel__body--flush tree";
  const tree = state.tree;
  if (!tree) {
    box.body.appendChild(loadingState("正在读取目录…"));
    return box;
  }
  const groups = tree.groups || [];

  const all = button("全部记录（" + fmtNum(state.records.total || 0) + "）", {
    variant: state.filters.groupId || state.filters.userId ? "ghost" : "primary",
    small: true,
    icon: "i-database",
    onClick: () => {
      state.filters.groupId = "";
      state.filters.userId = "";
      state.records.page = 1;
      void loadRecords();
    },
  });
  const allWrap = make("div", "tree__all");
  allWrap.appendChild(all);
  box.body.appendChild(allWrap);

  if (!groups.length) {
    box.body.appendChild(emptyState("还没有归档", "生成第一份画像后，这里会按群聊和群友自动分类。", "i-users"));
    return box;
  }

  for (const group of groups) {
    const wrap = make("div", "treegroup");
    const head = make("button", "treegroup__head");
    head.type = "button";
    const open = state.expanded.has(group.group_id);
    head.setAttribute("aria-expanded", open ? "true" : "false");
    head.dataset.active = state.filters.groupId === group.group_id && !state.filters.userId ? "true" : "false";
    const caret = icon("i-caret", 14);
    caret.classList.add("treegroup__caret");
    append(head, [
      caret,
      make("span", "treegroup__name", group.group_name),
      make("span", "treegroup__count", fmtNum(group.total)),
    ]);
    head.addEventListener("click", () => {
      if (state.expanded.has(group.group_id)) {
        state.expanded.delete(group.group_id);
      } else {
        state.expanded.add(group.group_id);
      }
      state.filters.groupId = group.group_id;
      state.filters.userId = "";
      state.records.page = 1;
      void loadRecords();
    });
    wrap.appendChild(head);

    if (open) {
      const members = make("div", "treemembers");
      for (const member of group.members || []) {
        const item = make("button", "treemember");
        item.type = "button";
        item.dataset.active =
          state.filters.groupId === group.group_id && state.filters.userId === member.user_id ? "true" : "false";
        item.title = member.user_name + "（" + member.user_id + "）· 最近 " + fmtAgo(member.latest);
        append(item, [
          icon("i-user", 13),
          make("span", "treemember__name", member.user_name),
          make("span", "treemember__count", fmtNum(member.total)),
        ]);
        item.addEventListener("click", () => {
          state.filters.groupId = group.group_id;
          state.filters.userId = member.user_id;
          state.records.page = 1;
          void loadRecords();
        });
        members.appendChild(item);
      }
      wrap.appendChild(members);
    }
    box.body.appendChild(wrap);
  }
  return box;
}

function recordCard(item) {
  const btn = make("button", "record");
  btn.type = "button";

  const top = make("div", "record__top");
  const who = make("div", "record__who");
  append(who, [
    make("span", "record__name", item.user_name),
    make("span", "record__id", "#" + item.user_id),
  ]);
  const meta = make("div", "record__meta");
  append(meta, [
    pill(item.kind_label, "pill--accent"),
    pill(confidenceText(item.confidence) + " " + pct(item.confidence) + "%", confidenceTone(item.confidence)),
  ]);
  append(top, [who, meta]);
  btn.appendChild(top);

  if (item.headline) {
    btn.appendChild(make("p", "record__headline", item.headline));
  }

  if ((item.tags || []).length) {
    const tags = make("div", "taglist");
    for (const tag of item.tags) {
      tags.appendChild(pill(tag));
    }
    btn.appendChild(tags);
  }

  const foot = make("div", "record__foot");
  append(foot, [
    make("span", "", item.group_name),
    make("span", "", "样本 " + fmtNum(item.sample_size) + " 条"),
    make("span", "", "约 " + fmtNum(item.corpus_chars) + " 字"),
    make("span", "", item.has_card ? "已出图" : "纯文本"),
    make("span", "", fmtAgo(item.created_at)),
  ]);
  btn.appendChild(foot);

  btn.addEventListener("click", () => {
    void openDetail(item.id);
  });
  return btn;
}

function renderRecords(root) {
  const layout = make("div", "records");
  layout.appendChild(renderTree());

  const search = make("div", "searchbox");
  const input = make("input", "input");
  input.type = "search";
  input.placeholder = "搜昵称、群号或画像内容";
  input.value = state.filters.q;
  input.addEventListener("change", () => {
    state.filters.q = input.value.trim();
    state.records.page = 1;
    void loadRecords();
  });
  append(search, [icon("i-search", 15), input]);

  const kindSelect = make("select", "select");
  kindSelect.style.width = "auto";
  for (const option of KIND_FILTERS) {
    const opt = make("option", "", option.label);
    opt.value = option.value;
    kindSelect.appendChild(opt);
  }
  for (const custom of state.prompts ? state.prompts.custom || [] : []) {
    const opt = make("option", "", (custom.label || custom.key) + "（自定义）");
    opt.value = custom.key;
    kindSelect.appendChild(opt);
  }
  kindSelect.value = state.filters.kind;
  kindSelect.addEventListener("change", () => {
    state.filters.kind = kindSelect.value;
    state.records.page = 1;
    void loadRecords();
  });

  const tools = [search, kindSelect];
  if (state.filters.groupId || state.filters.userId) {
    tools.push(
      button("清空当前范围", {
        variant: "danger",
        small: true,
        icon: "i-trash",
        onClick: () => void purgeScope(),
      }),
    );
  }

  const list = panel("画像记录", scopeLabel() + " · 共 " + fmtNum(state.records.total) + " 条", "i-records", tools);
  list.body.className = "panel__body panel__body--flush";

  if (state.busy && !state.records.items.length) {
    list.body.appendChild(loadingState("正在读取记录…"));
  } else if (!state.records.items.length) {
    list.body.appendChild(
      emptyState(
        "这个范围里没有记录",
        "换个筛选条件，或者到群里用「棱镜画像 @某人」生成一份新的。",
        "i-records",
      ),
    );
  } else {
    const wrap = make("div", "recordlist");
    for (const item of state.records.items) {
      wrap.appendChild(recordCard(item));
    }
    list.body.appendChild(wrap);

    const pager = make("div", "pager");
    const prev = button("上一页", {
      small: true,
      onClick: () => {
        if (state.records.page > 1) {
          state.records.page -= 1;
          void loadRecords();
        }
      },
    });
    const next = button("下一页", {
      small: true,
      onClick: () => {
        if (state.records.page < state.records.pages) {
          state.records.page += 1;
          void loadRecords();
        }
      },
    });
    prev.disabled = state.records.page <= 1;
    next.disabled = state.records.page >= state.records.pages;
    append(pager, [prev, make("span", "", state.records.page + " / " + state.records.pages), next]);
    list.body.appendChild(pager);
  }

  layout.appendChild(list);
  root.appendChild(layout);
}

/* ---------------------------------------------------------------- 详情抽屉 */

function polarityTone(polarity) {
  if (polarity === "positive" || polarity === "good") {
    return "pill--ok";
  }
  if (polarity === "negative" || polarity === "bad") {
    return "pill--danger";
  }
  return "";
}

function closeDrawer() {
  state.detail = null;
  dom.drawer.hidden = true;
  dom.scrim.hidden = true;
  clear(dom.drawer);
}

async function openDetail(recordId) {
  try {
    const data = await apiGet("dashboard/record", { id: recordId });
    state.detail = data.record;
    renderDrawer();
    void loadCardPreview(recordId);
  } catch (err) {
    toast(err.message, "err");
  }
}

async function loadCardPreview(recordId) {
  const slot = dom.drawer.querySelector("[data-card-slot]");
  if (!slot) {
    return;
  }
  try {
    const data = await apiGet("dashboard/record-card", { id: recordId });
    clear(slot);
    if (!data.data_url) {
      slot.appendChild(make("p", "field__hint", data.reason || "该记录没有可预览的图片。"));
      return;
    }
    const img = make("img");
    img.src = data.data_url;
    img.alt = "画像卡片预览";
    img.loading = "lazy";
    slot.appendChild(img);
    slot.appendChild(make("p", "field__hint", "卡片文件约 " + fmtBytes(data.bytes)));
  } catch (err) {
    clear(slot);
    slot.appendChild(make("p", "field__hint", "预览失败：" + err.message));
  }
}

function renderDrawer() {
  const record = state.detail;
  if (!record) {
    closeDrawer();
    return;
  }
  const payload = record.payload || {};
  clear(dom.drawer);

  const head = make("header", "drawer__head");
  const text = make("div", "panel__head-text");
  append(text, [
    make("p", "kicker", record.kind_label + " · " + (record.theme_label || record.theme)),
    make("h2", "", record.user_name),
    make("p", "field__hint", record.group_name + " · #" + record.user_id + " · " + fmtTime(record.created_at)),
  ]);
  const close = button("", { variant: "ghost", icon: "i-close", title: "关闭" });
  close.classList.add("btn--icon");
  close.style.marginLeft = "auto";
  close.addEventListener("click", closeDrawer);
  append(head, [text, close]);
  dom.drawer.appendChild(head);

  const body = make("div", "drawer__body");

  const metaTags = make("div", "taglist");
  append(metaTags, [
    pill(confidenceText(record.confidence) + " " + pct(record.confidence) + "%", confidenceTone(record.confidence)),
    pill("样本 " + fmtNum(record.sample_size) + " 条"),
    pill("约 " + fmtNum(record.corpus_chars) + " 字"),
    record.model ? pill(record.model, "pill--mono") : null,
    payload.structured === false ? pill("非结构化输出", "pill--warn") : null,
  ]);
  body.appendChild(metaTags);

  if (payload.headline) {
    body.appendChild(make("p", "headline", payload.headline));
  }

  if ((payload.tags || []).length) {
    const block = make("div", "block");
    block.appendChild(make("p", "block__title", "标签"));
    const tags = make("div", "taglist");
    for (const tag of payload.tags) {
      tags.appendChild(pill(tag.label, polarityTone(tag.polarity)));
    }
    block.appendChild(tags);
    body.appendChild(block);
  }

  if ((payload.dimensions || []).length) {
    const block = make("div", "block");
    block.appendChild(make("p", "block__title", "维度评分"));
    for (const dim of payload.dimensions) {
      const item = make("div", "dimension");
      const dimHead = make("div", "dimension__head");
      append(dimHead, [make("strong", "", dim.name), make("span", "bar__num", String(dim.score))]);
      const track = make("div", "meter__track");
      const fill = make("div", "meter__fill");
      fill.style.width = Math.max(0, Math.min(100, Number(dim.score || 0))) + "%";
      track.appendChild(fill);
      append(item, [dimHead, track, dim.note ? make("p", "dimension__note", dim.note) : null]);
      block.appendChild(item);
    }
    body.appendChild(block);
  }

  if ((payload.sections || []).length) {
    const block = make("div", "block");
    block.appendChild(make("p", "block__title", "分析正文"));
    for (const section of payload.sections) {
      const box = make("div", "section");
      append(box, [make("h4", "", section.title), make("p", "", section.body)]);
      block.appendChild(box);
    }
    body.appendChild(block);
  }

  if ((payload.evidence || []).length) {
    const block = make("div", "block");
    block.appendChild(make("p", "block__title", "原话证据"));
    for (const item of payload.evidence) {
      const box = make("div", "quote");
      const quote = make("blockquote", "", item.quote);
      append(box, [quote, item.reason ? make("cite", "", item.reason) : null]);
      block.appendChild(box);
    }
    body.appendChild(block);
  }

  if ((payload.advice || []).length) {
    const block = make("div", "block");
    block.appendChild(make("p", "block__title", "相处建议"));
    const list = make("ul", "advice");
    for (const line of payload.advice) {
      list.appendChild(make("li", "", line));
    }
    block.appendChild(list);
    body.appendChild(block);
  }

  const cardBlock = make("div", "block");
  cardBlock.appendChild(make("p", "block__title", "卡片预览"));
  const cardBox = make("div", "cardpreview");
  const slot = make("div");
  slot.dataset.cardSlot = "1";
  slot.appendChild(make("p", "field__hint", record.has_card ? "正在载入图片…" : "该记录以纯文本形式发出。"));
  cardBox.appendChild(slot);
  cardBlock.appendChild(cardBox);
  body.appendChild(cardBlock);

  if (record.text) {
    const block = make("div", "block");
    block.appendChild(make("p", "block__title", "发送的文本"));
    block.appendChild(make("pre", "rawtext", record.text));
    body.appendChild(block);
  }

  if (payload.raw_text && payload.structured === false) {
    const block = make("div", "block");
    block.appendChild(make("p", "block__title", "模型原始输出"));
    block.appendChild(make("pre", "rawtext", payload.raw_text));
    body.appendChild(block);
  }

  dom.drawer.appendChild(body);

  const foot = make("div", "drawer__foot");
  foot.appendChild(make("span", "field__hint", "记录 #" + record.id));
  foot.appendChild(make("span", "spacer"));
  foot.appendChild(
    button("屏蔽此人", {
      small: true,
      icon: "i-eye-off",
      title: "把这位群友加入自助隐身名单，后续不再生成画像",
      onClick: () => void addOptout(record),
    }),
  );
  foot.appendChild(
    button("删除记录", {
      variant: "danger",
      small: true,
      icon: "i-trash",
      onClick: () => void deleteRecord(record.id),
    }),
  );
  dom.drawer.appendChild(foot);

  dom.drawer.hidden = false;
  dom.scrim.hidden = false;
}

/* -------------------------------------------------------------- 设置视图 */

function currentValue(path) {
  if (Object.prototype.hasOwnProperty.call(state.draft, path)) {
    return state.draft[path];
  }
  return (state.settings.values || {})[path];
}

function setDraft(path, value) {
  const original = (state.settings.values || {})[path];
  const same = JSON.stringify(original) === JSON.stringify(value);
  if (same) {
    delete state.draft[path];
  } else {
    state.draft[path] = value;
  }
  refreshSaveBar();
}

function refreshSaveBar() {
  const bar = document.getElementById("savebar");
  if (!bar) {
    return;
  }
  const count = Object.keys(state.draft).length;
  bar.hidden = count === 0;
  const label = bar.querySelector("[data-savebar-text]");
  if (label) {
    label.textContent = count + " 项待保存";
  }
}

function fieldBox(field) {
  const box = make("div", "field");
  const label = make("label", "field__label");
  label.appendChild(make("span", "", field.label));
  box.appendChild(label);
  const value = currentValue(field.path);

  if (field.type === "bool") {
    const wrap = make("label", "switch");
    const input = make("input");
    input.type = "checkbox";
    input.checked = Boolean(value);
    const track = make("span", "switch__track");
    const caption = make("span", "", Boolean(value) ? "已开启" : "已关闭");
    input.addEventListener("change", () => {
      caption.textContent = input.checked ? "已开启" : "已关闭";
      setDraft(field.path, input.checked);
    });
    append(wrap, [input, track, caption]);
    box.appendChild(wrap);
  } else if (field.type === "int") {
    const input = make("input", "input");
    input.type = "number";
    input.step = "1";
    input.value = String(Number(value || 0));
    input.addEventListener("change", () => {
      const parsed = Number.parseInt(input.value, 10);
      setDraft(field.path, Number.isFinite(parsed) ? parsed : Number(field.default || 0));
    });
    box.appendChild(input);
  } else if (field.type === "choice") {
    const select = make("select", "select");
    for (const choice of field.choices || []) {
      const opt = make("option", "", choice.label);
      opt.value = choice.value;
      select.appendChild(opt);
    }
    select.value = String(value === undefined ? field.default : value);
    const hintLine = make("p", "field__hint", "");
    const syncHint = () => {
      const hit = (field.choices || []).find((c) => c.value === select.value);
      hintLine.textContent = hit && hit.hint ? hit.hint : "";
    };
    syncHint();
    select.addEventListener("change", () => {
      syncHint();
      setDraft(field.path, select.value);
    });
    append(box, [select, hintLine]);
  } else if (field.type === "multi") {
    const picked = new Set(Array.isArray(value) ? value.map(String) : []);
    const grid = make("div", "checkgrid");
    for (const choice of field.choices || []) {
      const chip = make("label", "checkchip");
      chip.dataset.on = picked.has(String(choice.value)) ? "true" : "false";
      const input = make("input");
      input.type = "checkbox";
      input.checked = picked.has(String(choice.value));
      input.addEventListener("change", () => {
        if (input.checked) {
          picked.add(String(choice.value));
        } else {
          picked.delete(String(choice.value));
        }
        chip.dataset.on = input.checked ? "true" : "false";
        setDraft(field.path, Array.from(picked));
      });
      append(chip, [input, make("span", "", choice.label)]);
      grid.appendChild(chip);
    }
    box.appendChild(grid);
  } else if (field.type === "list") {
    const area = make("textarea", "textarea");
    area.rows = 4;
    area.placeholder = "每行一项，留空表示不限制";
    area.value = (Array.isArray(value) ? value : []).join("\n");
    area.addEventListener("change", () => {
      const lines = area.value
        .split("\n")
        .map((line) => line.trim())
        .filter((line) => line.length > 0);
      setDraft(field.path, lines);
    });
    box.appendChild(area);
  } else {
    const input = make("input", "input");
    input.type = "text";
    input.value = value === undefined || value === null ? "" : String(value);
    input.addEventListener("change", () => {
      setDraft(field.path, input.value.trim());
    });
    box.appendChild(input);
  }

  if (field.hint) {
    box.appendChild(make("p", "field__hint", field.hint));
  }
  return box;
}

function renderOptouts(root) {
  const data = state.settings;
  const box = panel("自助隐身名单", "名单内的成员不会被生成画像", "i-eye-off");
  box.body.className = "panel__body";

  const form = make("div", "inlineform");
  const gidField = make("div", "field");
  const gidInput = make("input", "input");
  gidInput.placeholder = "群号（留空=全局）";
  append(gidField, [make("label", "field__label", "群号"), gidInput]);
  const uidField = make("div", "field");
  const uidInput = make("input", "input");
  uidInput.placeholder = "必填";
  append(uidField, [make("label", "field__label", "用户号"), uidInput]);
  const nameField = make("div", "field");
  const nameInput = make("input", "input");
  nameInput.placeholder = "便于识别";
  append(nameField, [make("label", "field__label", "备注名"), nameInput]);
  const addBtn = button("加入名单", {
    variant: "primary",
    icon: "i-plus",
    onClick: async () => {
      const userId = uidInput.value.trim();
      if (!userId) {
        toast("请填写用户号。", "err");
        return;
      }
      try {
        await apiPost("dashboard/optout", {
          action: "add",
          platform: "aiocqhttp",
          group_id: gidInput.value.trim(),
          user_id: userId,
          user_name: nameInput.value.trim(),
        });
        toast("已加入隐身名单。", "ok");
        gidInput.value = "";
        uidInput.value = "";
        nameInput.value = "";
        await loadSettings();
      } catch (err) {
        toast(err.message, "err");
      }
    },
  });
  append(form, [gidField, uidField, nameField, addBtn]);
  box.body.appendChild(form);

  const list = make("div");
  const rows = data.optouts || [];
  if (!rows.length) {
    list.appendChild(emptyState("名单为空", "群友可以自己发「棱镜隐身」退出，管理员也能在上面手动添加。", "i-eye-off"));
  } else {
    for (const row of rows) {
      const item = make("div", "optout");
      const info = make("div");
      append(info, [
        make("div", "optout__name", row.user_name),
        make("div", "optout__meta", "#" + row.user_id + " · " + (row.group_id || "全局") + " · " + row.platform),
      ]);
      append(item, [
        icon("i-user", 16),
        info,
        pill(row.reason === "self" ? "本人申请" : "后台添加"),
        pill(fmtAgo(row.created_at), "pill--muted"),
        button("移出", {
          small: true,
          onClick: async () => {
            try {
              await apiPost("dashboard/optout", {
                action: "remove",
                platform: row.platform,
                group_id: row.group_id,
                user_id: row.user_id,
              });
              toast("已移出名单。", "ok");
              await loadSettings();
            } catch (err) {
              toast(err.message, "err");
            }
          },
        }),
      ]);
      list.appendChild(item);
    }
  }
  box.body.appendChild(list);
  root.appendChild(box);
}

function renderSettings(root) {
  const data = state.settings;
  if (!data) {
    root.appendChild(loadingState("正在读取配置…"));
    return;
  }

  root.appendChild(
    notice(
      "这里改的是插件运行配置，保存后立即生效并写回 AstrBot 配置文件。少数高风险开关（人格克隆是否同步机器人昵称与头像、数据库上限等）只能在 AstrBot 插件配置页修改，避免误触。",
      "notice--info",
      "i-shield",
    ),
  );

  const readonly = data.readonly || {};
  if (readonly["persona_clone.sync_bot_nickname"] || readonly["persona_clone.sync_bot_avatar"]) {
    root.appendChild(
      notice(
        "当前已允许「棱镜克隆」改动机器人的全局昵称或头像。这个动作不可自动撤销，建议仅在自用机器人上开启。",
        "notice--danger",
      ),
    );
  }

  for (const section of data.sections || []) {
    const box = panel(section.label, "共 " + (section.fields || []).length + " 项", "i-" + (section.icon || "sliders"));
    const grid = make("div", "fieldgrid");
    for (const field of section.fields || []) {
      const cell = fieldBox(field);
      if (field.type === "list" || field.type === "multi") {
        cell.style.gridColumn = "1 / -1";
      }
      grid.appendChild(cell);
    }
    box.body.appendChild(grid);
    root.appendChild(box);
  }

  renderOptouts(root);

  const bar = make("div", "savebar");
  bar.id = "savebar";
  bar.hidden = Object.keys(state.draft).length === 0;
  const barText = make("span", "savebar__text", Object.keys(state.draft).length + " 项待保存");
  barText.dataset.savebarText = "1";
  append(bar, [
    icon("i-save", 16),
    barText,
    make("span", "spacer"),
    button("放弃修改", {
      onClick: () => {
        state.draft = {};
        render();
      },
    }),
    button("保存并生效", { variant: "primary", icon: "i-check", onClick: () => void saveSettings() }),
  ]);
  root.appendChild(bar);
}

/* ------------------------------------------------------------ 提示词视图 */

function promptEditor(entry) {
  const draft = {
    key: entry ? entry.key : "",
    command: entry ? entry.command : "",
    label: entry ? entry.label : "",
    prompt: entry ? entry.prompt : "",
    structured: entry ? entry.structured !== false : true,
    enabled: entry ? entry.enabled !== false : true,
  };
  const isNew = !entry;
  const box = panel(
    isNew ? "新建自定义模板" : "编辑：" + draft.label,
    "模板会注册成一条群内指令，用法与内置指令一致",
    "i-prompt",
  );

  const grid = make("div", "fieldgrid");

  const keyField = make("div", "field");
  const keyInput = make("input", "input");
  keyInput.value = draft.key;
  keyInput.placeholder = "例如 workstyle";
  keyInput.disabled = !isNew;
  keyInput.addEventListener("input", () => {
    draft.key = keyInput.value.trim();
  });
  append(keyField, [
    make("label", "field__label", "标识 key"),
    keyInput,
    make("p", "field__hint", isNew ? "字母、数字、下划线或连字符；保存后不可修改。" : "已有模板的标识不可修改。"),
  ]);

  const cmdField = make("div", "field");
  const cmdInput = make("input", "input");
  cmdInput.value = draft.command;
  cmdInput.placeholder = "例如 棱镜工牌";
  cmdInput.addEventListener("input", () => {
    draft.command = cmdInput.value.trim();
  });
  append(cmdField, [
    make("label", "field__label", "触发指令"),
    cmdInput,
    make("p", "field__hint", "不能有空格，也不能和内置指令重名。"),
  ]);

  const labelField = make("div", "field");
  const labelInput = make("input", "input");
  labelInput.value = draft.label;
  labelInput.placeholder = "例如 工作风格画像";
  labelInput.addEventListener("input", () => {
    draft.label = labelInput.value.trim();
  });
  append(labelField, [
    make("label", "field__label", "显示名称"),
    labelInput,
    make("p", "field__hint", "会印在卡片标题上。"),
  ]);

  const flagField = make("div", "field");
  const structWrap = make("label", "switch");
  const structInput = make("input");
  structInput.type = "checkbox";
  structInput.checked = draft.structured;
  const structCaption = make("span", "", draft.structured ? "结构化输出（渲染卡片）" : "自由文本（直接发文字）");
  structInput.addEventListener("change", () => {
    draft.structured = structInput.checked;
    structCaption.textContent = structInput.checked ? "结构化输出（渲染卡片）" : "自由文本（直接发文字）";
  });
  append(structWrap, [structInput, make("span", "switch__track"), structCaption]);

  const enabledWrap = make("label", "switch");
  const enabledInput = make("input");
  enabledInput.type = "checkbox";
  enabledInput.checked = draft.enabled;
  const enabledCaption = make("span", "", draft.enabled ? "已启用" : "已停用");
  enabledInput.addEventListener("change", () => {
    draft.enabled = enabledInput.checked;
    enabledCaption.textContent = enabledInput.checked ? "已启用" : "已停用";
  });
  append(enabledWrap, [enabledInput, make("span", "switch__track"), enabledCaption]);

  append(flagField, [make("label", "field__label", "输出形态"), structWrap, enabledWrap]);

  append(grid, [keyField, cmdField, labelField, flagField]);
  box.body.appendChild(grid);

  const promptField = make("div", "field");
  const area = make("textarea", "textarea");
  area.rows = 14;
  area.value = draft.prompt;
  area.placeholder = "写清楚你要模型输出什么。可用占位符：{target_name} {group_name} {corpus} {stats} {profile} {schema}";
  area.addEventListener("input", () => {
    draft.prompt = area.value;
  });
  append(promptField, [
    make("label", "field__label", "提示词正文"),
    area,
    make(
      "p",
      "field__hint",
      "语料、统计特征、资料字段会自动拼在提示词后面；结构化模板还会自动附加 JSON 输出格式约束，不必自己写。",
    ),
  ]);
  box.body.appendChild(promptField);

  const actions = make("div", "inlineform");
  actions.appendChild(
    button("保存模板", {
      variant: "primary",
      icon: "i-save",
      onClick: () => void savePrompt(draft),
    }),
  );
  actions.appendChild(
    button("取消", {
      onClick: () => {
        state.promptDraft = null;
        render();
      },
    }),
  );
  box.body.appendChild(actions);
  return box;
}

function promptCard(entry, builtin) {
  const card = make("article", "promptcard");
  const top = make("div", "promptcard__top");
  append(top, [
    make("strong", "", entry.label || entry.key),
    pill(entry.command, "pill--accent pill--mono"),
    pill(entry.structured === false ? "自由文本" : "结构化卡片"),
    builtin ? pill("内置", "pill--muted") : pill(entry.enabled === false ? "已停用" : "已启用", entry.enabled === false ? "pill--warn" : "pill--ok"),
  ]);
  const tools = make("div", "promptcard__tools");
  if (builtin) {
    tools.appendChild(
      button("以此为模板新建", {
        small: true,
        icon: "i-plus",
        onClick: () => {
          state.promptDraft = {
            key: "",
            command: "",
            label: entry.label + " 副本",
            prompt: entry.prompt,
            structured: entry.structured !== false,
            enabled: true,
          };
          render();
        },
      }),
    );
  } else {
    tools.appendChild(
      button("编辑", {
        small: true,
        onClick: () => {
          state.promptDraft = Object.assign({}, entry);
          render();
        },
      }),
    );
    tools.appendChild(
      button("删除", {
        variant: "danger",
        small: true,
        icon: "i-trash",
        onClick: () => void deletePrompt(entry.key),
      }),
    );
  }
  top.appendChild(tools);
  card.appendChild(top);
  card.appendChild(make("pre", "", entry.prompt));
  if (!builtin && entry.updated_at) {
    card.appendChild(make("p", "field__hint", "更新于 " + fmtTime(entry.updated_at)));
  }
  return card;
}

function renderPrompts(root) {
  const data = state.prompts;
  if (!data) {
    root.appendChild(loadingState("正在读取模板…"));
    return;
  }

  if (state.promptDraft) {
    root.appendChild(promptEditor(state.promptDraft.key ? state.promptDraft : null));
    if (!state.promptDraft.key) {
      root.appendChild(
        notice("新建模板时先想清楚指令名，保存后标识不可再改；如果只是想微调语气，直接从下面的内置模板「以此为模板新建」更省事。", "notice--info", "i-spark"),
      );
    }
    return;
  }

  const custom = data.custom || [];
  const customPanel = panel(
    "自定义模板",
    custom.length ? "已注册 " + custom.length + " 套，指令即时生效" : "还没有自定义模板",
    "i-prompt",
    [
      button("新建模板", {
        variant: "primary",
        small: true,
        icon: "i-plus",
        onClick: () => {
          state.promptDraft = { key: "", command: "", label: "", prompt: "", structured: true, enabled: true };
          render();
        },
      }),
    ],
  );
  if (!custom.length) {
    customPanel.body.appendChild(
      emptyState(
        "你可以加自己的玩法",
        "比如「棱镜工牌」输出职场画像、「棱镜宠物」把群友写成一只动物。写好提示词保存后，群里直接发指令就能用。",
        "i-prompt",
      ),
    );
  } else {
    const grid = make("div", "promptgrid");
    for (const entry of custom) {
      grid.appendChild(promptCard(entry, false));
    }
    customPanel.body.appendChild(grid);
  }
  root.appendChild(customPanel);

  const builtinPanel = panel("内置模板", "只读，作为写法参考", "i-layers");
  const bGrid = make("div", "promptgrid");
  for (const entry of data.builtin || []) {
    bGrid.appendChild(promptCard(entry, true));
  }
  builtinPanel.body.appendChild(bGrid);
  root.appendChild(builtinPanel);

  if ((data.reserved_commands || []).length) {
    const box = panel("已占用的指令", "新建模板时请避开这些名字", "i-shield");
    const tags = make("div", "taglist");
    for (const cmd of data.reserved_commands) {
      tags.appendChild(pill(cmd, "pill--mono"));
    }
    box.body.appendChild(tags);
    root.appendChild(box);
  }
}

/* ---------------------------------------------------------------- 数据动作 */

async function loadOverview() {
  try {
    state.overview = await apiGet("dashboard/overview");
  } catch (err) {
    toast(err.message, "err");
  }
}

async function loadTree() {
  try {
    state.tree = await apiGet("dashboard/groups");
  } catch (err) {
    state.tree = { groups: [], total_groups: 0, total_members: 0 };
    toast(err.message, "err");
  }
}

async function loadRecords() {
  state.busy = true;
  render();
  try {
    const data = await apiGet("dashboard/records", {
      page: state.records.page,
      size: PAGE_SIZE,
      group_id: state.filters.groupId,
      user_id: state.filters.userId,
      kind: state.filters.kind,
      q: state.filters.q,
    });
    state.records = {
      items: data.items || [],
      page: data.page || 1,
      pages: Math.max(1, data.pages || 1),
      total: data.total || 0,
    };
  } catch (err) {
    toast(err.message, "err");
  } finally {
    state.busy = false;
    render();
  }
}

async function loadSettings() {
  try {
    state.settings = await apiGet("dashboard/settings");
  } catch (err) {
    toast(err.message, "err");
  }
  render();
}

async function saveSettings() {
  const pending = Object.keys(state.draft);
  if (!pending.length) {
    toast("当前没有待保存的修改。", "info");
    return;
  }
  try {
    const data = await apiPost("dashboard/settings", { values: state.draft });
    state.draft = {};
    const applied = typeof data.applied === "number" ? data.applied : pending.length;
    toast(applied ? "已保存 " + applied + " 项设置，立即生效。" : "填的值和当前配置一样，没有改动。", "ok");
    await loadSettings();
    await loadOverview();
    render();
  } catch (err) {
    toast(err.message, "err");
  }
}

async function loadPrompts() {
  try {
    state.prompts = await apiGet("dashboard/prompts");
  } catch (err) {
    toast(err.message, "err");
  }
  render();
}

async function savePrompt(draft) {
  if (!draft) {
    return;
  }
  try {
    await apiPost("dashboard/prompts", {
      key: draft.key,
      command: draft.command,
      label: draft.label,
      prompt: draft.prompt,
      structured: draft.structured !== false,
      enabled: draft.enabled !== false,
    });
    state.promptDraft = null;
    toast("模板已保存，群里马上就能用这条指令。", "ok");
    await loadPrompts();
  } catch (err) {
    toast(err.message, "err");
  }
}

async function deletePrompt(key) {
  const ok = window.confirm(
    "确认删除模板「" + key + "」？对应的群内指令会立刻失效，已经生成的画像记录不受影响。",
  );
  if (!ok) {
    return;
  }
  try {
    await apiPost("dashboard/prompts", { action: "delete", key: key });
    state.promptDraft = null;
    toast("模板已删除。", "ok");
    await loadPrompts();
  } catch (err) {
    toast(err.message, "err");
  }
}

async function deleteRecord(id) {
  if (!window.confirm("确认删除这条画像记录？对应的卡片图片会一起删掉，无法恢复。")) {
    return;
  }
  try {
    const data = await apiPost("dashboard/record-delete", { id: id });
    closeDrawer();
    toast("已删除 " + (data.deleted || 1) + " 条记录。", "ok");
    await loadRecords();
    await loadTree();
    await loadOverview();
    render();
  } catch (err) {
    toast(err.message, "err");
  }
}

async function purgeScope() {
  const groupId = state.filters.groupId;
  const userId = state.filters.userId;
  if (!groupId && !userId) {
    toast("请先在左侧选中一个群或群友，避免一次删掉全部记录。", "err");
    return;
  }
  const ok = window.confirm(
    "将删除【" + scopeLabel() + "】范围内的所有画像记录和卡片图片，操作不可撤销。确认继续？",
  );
  if (!ok) {
    return;
  }
  try {
    const data = await apiPost("dashboard/records-purge", { group_id: groupId, user_id: userId });
    toast("已清理 " + (data.deleted || 0) + " 条记录。", "ok");
    state.records.page = 1;
    closeDrawer();
    await loadRecords();
    await loadTree();
    await loadOverview();
    render();
  } catch (err) {
    toast(err.message, "err");
  }
}

async function addOptout(record) {
  if (!record || !record.user_id) {
    return;
  }
  const who = record.user_name || record.user_id;
  const ok = window.confirm(
    "把「" + who + "」加入隐身名单？之后群里不能再对 TA 发起画像，本人发「棱镜现身」可以自己解除。",
  );
  if (!ok) {
    return;
  }
  try {
    await apiPost("dashboard/optout", {
      action: "add",
      platform: record.platform || "aiocqhttp",
      group_id: record.group_id || "",
      user_id: record.user_id,
      user_name: record.user_name || "",
    });
    toast("已加入隐身名单。", "ok");
    if (state.settings) {
      await loadSettings();
    }
    await loadOverview();
    render();
  } catch (err) {
    toast(err.message, "err");
  }
}

/* ------------------------------------------------------------ 导航与渲染 */

function navBadge(viewId) {
  if (viewId === "overview") {
    const stats = state.overview ? state.overview.stats || {} : {};
    return stats.portraits ? fmtNum(stats.portraits) : "";
  }
  if (viewId === "records") {
    return state.records.total ? fmtNum(state.records.total) : "";
  }
  if (viewId === "prompts") {
    const custom = state.prompts ? state.prompts.custom || [] : [];
    return custom.length ? String(custom.length) : "";
  }
  if (viewId === "settings") {
    const pending = Object.keys(state.draft).length;
    return pending ? pending + " 改" : "";
  }
  return "";
}

function renderNav() {
  clear(dom.nav);
  for (const view of VIEWS) {
    const btn = make("button", "navitem");
    btn.type = "button";
    btn.title = view.label;
    btn.setAttribute("aria-current", view.id === state.view ? "true" : "false");
    append(btn, [icon(view.icon, 18), make("span", "", view.label)]);
    const badge = navBadge(view.id);
    if (badge) {
      btn.appendChild(make("span", "navitem__badge", badge));
    }
    btn.addEventListener("click", () => {
      switchView(view.id);
    });
    dom.nav.appendChild(btn);
  }
}

function render() {
  const view = VIEWS.find((item) => item.id === state.view) || VIEWS[0];
  dom.viewTitle.textContent = view.label;
  dom.viewKicker.textContent = view.kicker;
  clear(dom.main);
  const root = make("div", "view");
  if (state.view === "records") {
    renderRecords(root);
  } else if (state.view === "settings") {
    renderSettings(root);
  } else if (state.view === "prompts") {
    renderPrompts(root);
  } else {
    renderOverview(root);
  }
  dom.main.appendChild(root);
  renderNav();
  refreshSaveBar();
}

function switchView(viewId) {
  if (viewId === state.view) {
    return;
  }
  if (state.view === "settings" && Object.keys(state.draft).length) {
    if (!window.confirm("设置里还有未保存的修改，离开会丢弃它们。确认离开？")) {
      return;
    }
    state.draft = {};
  }
  if (state.view === "prompts" && state.promptDraft) {
    if (!window.confirm("提示词编辑器里还有未保存的内容，离开会丢弃。确认离开？")) {
      return;
    }
    state.promptDraft = null;
  }
  state.view = viewId;
  closeDrawer();
  render();
  void ensureViewData(viewId);
}

async function ensureViewData(viewId) {
  if (viewId === "records") {
    if (!state.tree) {
      await loadTree();
      render();
    }
    if (!state.records.items.length && !state.records.total) {
      await loadRecords();
    }
    return;
  }
  if (viewId === "settings" && !state.settings) {
    await loadSettings();
    return;
  }
  if (viewId === "prompts" && !state.prompts) {
    await loadPrompts();
    return;
  }
  if (viewId === "overview" && !state.overview) {
    await loadOverview();
    render();
  }
}

async function refreshCurrentView() {
  dom.refresh.disabled = true;
  try {
    if (state.view === "records") {
      await loadTree();
      await loadRecords();
    } else if (state.view === "settings") {
      state.draft = {};
      await loadSettings();
    } else if (state.view === "prompts") {
      state.promptDraft = null;
      await loadPrompts();
    } else {
      await loadOverview();
      render();
    }
    if (state.detail && state.detail.id) {
      await openDetail(state.detail.id);
    }
    toast("已刷新。", "ok");
  } finally {
    dom.refresh.disabled = false;
  }
}

/* ---------------------------------------------------------------- 事件绑定 */

function bindEvents() {
  dom.refresh.addEventListener("click", () => {
    void refreshCurrentView();
  });

  dom.themeToggle.addEventListener("click", (event) => {
    event.stopPropagation();
    toggleThemeMenu();
  });

  document.addEventListener("click", (event) => {
    if (dom.themeMenu.hidden) {
      return;
    }
    if (!dom.themeMenu.contains(event.target) && !dom.themeToggle.contains(event.target)) {
      toggleThemeMenu(false);
    }
  });

  dom.scrim.addEventListener("click", () => {
    closeDrawer();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") {
      return;
    }
    if (!dom.themeMenu.hidden) {
      toggleThemeMenu(false);
      return;
    }
    if (!dom.drawer.hidden) {
      closeDrawer();
    }
  });

  if (window.matchMedia) {
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => {
      if (state.theme === "auto") {
        document.documentElement.dataset.theme = resolveTheme("auto");
      }
    };
    if (query.addEventListener) {
      query.addEventListener("change", onChange);
    } else if (query.addListener) {
      query.addListener(onChange);
    }
  }

  window.addEventListener("beforeunload", (event) => {
    if (Object.keys(state.draft).length || state.promptDraft) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
}

/* -------------------------------------------------------------------- 引导 */

function showBootFailure() {
  clear(dom.main);
  const box = make("div", "bootfail");
  const mark = make("span", "brand__mark");
  mark.appendChild(icon("i-alert", 22));
  append(box, [
    mark,
    make("h2", "", "页面桥接没有加载"),
    make(
      "p",
      "",
      "这个页面需要在 AstrBot 控制台内打开：进入「插件」页，找到「人格棱镜」，点开它的管理面板。" +
        "直接用浏览器打开 index.html 是拿不到数据的。",
    ),
  ]);
  dom.main.appendChild(box);
}

function cacheDom() {
  dom.nav = document.getElementById("nav");
  dom.main = document.getElementById("main");
  dom.viewTitle = document.getElementById("view-title");
  dom.viewKicker = document.getElementById("view-kicker");
  dom.themeToggle = document.getElementById("theme-toggle");
  dom.themeMenu = document.getElementById("theme-menu");
  dom.themeLabel = document.getElementById("theme-label");
  dom.refresh = document.getElementById("refresh");
  dom.scrim = document.getElementById("scrim");
  dom.drawer = document.getElementById("drawer");
  dom.toasts = document.getElementById("toasts");
  dom.versionPill = document.getElementById("version-pill");
}

function readStoredTheme() {
  let stored = "";
  try {
    stored = window.localStorage.getItem(THEME_KEY) || "";
  } catch (err) {
    stored = "";
  }
  return THEME_OPTIONS.some((item) => item.value === stored) ? stored : "auto";
}

async function boot() {
  cacheDom();
  applyTheme(readStoredTheme(), false);
  renderNav();

  if (!bridge) {
    showBootFailure();
    return;
  }

  bindEvents();
  render();

  try {
    if (typeof bridge.ready === "function") {
      await bridge.ready();
    }
  } catch (err) {
    toast("等待页面桥接就绪失败：" + err.message, "err");
  }

  await loadOverview();
  if (state.overview && state.overview.version) {
    dom.versionPill.textContent = state.overview.version;
  }
  await loadPrompts();
  state.booted = true;
  render();
}

void boot();
