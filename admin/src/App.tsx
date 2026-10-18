import { FormEvent, useEffect, useState } from "react";
import { Role, api, apiUrl, clearToken, getToken, setToken } from "./api";

type Row = Record<string, unknown>;

const RANK: Record<Role, number> = {
  support: 1,
  moderator: 2,
  administrator: 3,
  superadmin: 4,
};

const SECTIONS: { id: string; label: string; min: Role }[] = [
  { id: "stats", label: "Статистика", min: "support" },
  { id: "users", label: "Пользователи", min: "support" },
  { id: "profiles", label: "Анкеты", min: "support" },
  { id: "verify", label: "Верификация", min: "moderator" },
  { id: "reports", label: "Жалобы", min: "moderator" },
  { id: "chats", label: "Активные чаты", min: "moderator" },
  { id: "blocks", label: "Блокировки", min: "moderator" },
  { id: "appeals", label: "Обжалования", min: "moderator" },
  { id: "payments", label: "Платежи", min: "support" },
  { id: "wallet", label: "Балансы", min: "administrator" },
  { id: "referrals", label: "Рефералы", min: "support" },
  { id: "vip", label: "VIP", min: "support" },
  { id: "catalog", label: "Пакеты и VIP", min: "support" },
  { id: "gifts", label: "Подарки", min: "support" },
  { id: "tasks", label: "Задания", min: "support" },
  { id: "promos", label: "Промокоды", min: "administrator" },
  { id: "locales", label: "Переводы", min: "administrator" },
  { id: "settings", label: "Настройки", min: "administrator" },
  { id: "audit", label: "Журнал", min: "administrator" },
  { id: "errors", label: "Ошибки", min: "administrator" },
  { id: "admins", label: "Администраторы", min: "superadmin" },
];

function can(role: Role, min: Role): boolean {
  return RANK[role] >= RANK[min];
}

function Table({ rows }: { rows: Row[] }) {
  if (!rows.length) {
    return <p className="muted">Записей нет.</p>;
  }
  const keys = Object.keys(rows[0]);
  return (
    <div style={{ overflowX: "auto" }}>
      <table>
        <thead>
          <tr>
            {keys.map((key) => (
              <th key={key}>{key}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={String(row.id ?? index)}>
              {keys.map((key) => (
                <td key={key}>{formatCell(row[key])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function App() {
  const [token, setTokenState] = useState(getToken());
  const [role, setRole] = useState<Role | null>(null);
  const [username, setUsername] = useState("");
  const [section, setSection] = useState("stats");
  const [error, setError] = useState("");

  useEffect(() => {
    if (!token) return;
    api<{ role: Role; username: string }>("/api/auth/me")
      .then((me) => {
        setRole(me.role);
        setUsername(me.username);
      })
      .catch(() => {
        clearToken();
        setTokenState("");
      });
  }, [token]);

  if (!token || !role) {
    return <Login onSuccess={(value) => setTokenState(value)} />;
  }

  const visible = SECTIONS.filter((item) => can(role, item.min));
  const current = visible.some((item) => item.id === section) ? section : visible[0].id;

  return (
    <div className="app">
      <aside className="side">
        <div className="brand">
          ANONYMOUS LOVE
          <span>{username} · {role}</span>
        </div>
        <nav>
          {visible.map((item) => (
            <button key={item.id} className={item.id === current ? "active" : ""} onClick={() => { setError(""); setSection(item.id); }}>
              {item.label}
            </button>
          ))}
        </nav>
      </aside>
      <main className="main">
        <div className="top">
          <h2 style={{ margin: 0 }}>{visible.find((item) => item.id === current)?.label}</h2>
          <button className="ghost" onClick={() => { clearToken(); setTokenState(""); setRole(null); }}>Выйти</button>
        </div>
        {error && <p className="error">{error}</p>}
        <SectionView id={current} role={role} onError={setError} />
      </main>
    </div>
  );
}

function Login({ onSuccess }: { onSuccess: (token: string) => void }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      const result = await api<{ access_token: string }>("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      setToken(result.access_token);
      onSuccess(result.access_token);
    } catch (err) {
      setError(err instanceof Error ? err.message : "login failed");
    }
  }

  return (
    <div className="login-wrap">
      <form className="login" onSubmit={submit}>
        <h1>ANONYMOUS LOVE</h1>
        <p className="muted">Панель модерации. Вход только для сотрудников сервиса.</p>
        <input aria-label="Логин" value={username} onChange={(event) => setUsername(event.target.value)} placeholder="Логин" />
        <input aria-label="Пароль" type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Пароль" />
        {error && <p className="error">{error}</p>}
        <button className="primary" type="submit">Войти</button>
      </form>
    </div>
  );
}

function SectionView({ id, role, onError }: { id: string; role: Role; onError: (value: string) => void }) {
  const [rows, setRows] = useState<Row[]>([]);
  const [stats, setStats] = useState<Row | null>(null);
  const [query, setQuery] = useState("");
  const [reloadKey, setReloadKey] = useState(0);

  function reload() {
    setReloadKey((value) => value + 1);
  }

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        if (id === "stats") {
          const data = await api<Row>("/api/stats");
          if (!cancelled) setStats(data);
          return;
        }
        const path = pathFor(id, query);
        if (!path) return;
        const data = await api<Row[]>(path);
        if (!cancelled) setRows(data);
      } catch (err) {
        if (!cancelled) onError(err instanceof Error ? err.message : "request failed");
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [id, query, reloadKey, onError]);

  if (id === "verify") {
    return (
      <div className="panel">
        <VerificationQueue onError={onError} />
      </div>
    );
  }

  if (id === "stats" && stats) {
    return (
      <div className="cards">
        {Object.entries(stats).map(([key, value]) => (
          <div className="card" key={key}>
            <span className="muted">{key}</span>
            <b>{String(value)}</b>
          </div>
        ))}
        {can(role, "administrator") && (
          <button className="primary" onClick={() => downloadCsv()}>Экспорт сводки</button>
        )}
      </div>
    );
  }

  return (
    <div className="panel">
      {(id === "users" || id === "profiles") && (
        <form className="row" onSubmit={(event) => { event.preventDefault(); reload(); }}>
          <input aria-label="Поиск" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Внутренний ID или псевдоним" />
          <button className="primary" type="submit">Найти</button>
        </form>
      )}
      <Actions id={id} role={role} onDone={reload} onError={onError} />
      {id !== "catalog" && <Table rows={id === "profiles" ? rows.map(profileRow) : rows} />}
    </div>
  );
}

function profileRow(row: Row): Row {
  return {
    id: row.id,
    nickname: row.nickname,
    gender: row.gender,
    age: row.age,
    language: row.language,
    verification_status: row.verification_status,
    trust_score: row.trust_score,
    status: row.status,
  };
}

function pathFor(id: string, query: string): string {
  if (id === "users" || id === "profiles") return `/api/users?q=${encodeURIComponent(query)}`;
  if (id === "verify") return "/api/verifications?status=in_review";
  if (id === "reports") return "/api/reports?status=open";
  if (id === "chats") return "/api/chats/active";
  if (id === "blocks") return "/api/blocks";
  if (id === "appeals") return "/api/appeals";
  if (id === "payments") return "/api/payments";
  if (id === "wallet") return "/api/wallet/transactions";
  if (id === "referrals") return "/api/referrals";
  if (id === "vip") return "/api/subscriptions";
  if (id === "gifts") return "/api/gifts";
  if (id === "tasks") return "/api/tasks";
  if (id === "promos") return "/api/promos";
  if (id === "locales") return "/api/translations?lang=ru";
  if (id === "settings") return "/api/settings";
  if (id === "audit") return "/api/audit";
  if (id === "errors") return "/api/errors";
  if (id === "admins") return "/api/admins";
  return "";
}

async function downloadCsv() {
  const csv = await api<string>("/api/export/summary.csv");
  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "summary.csv";
  link.click();
  URL.revokeObjectURL(url);
}

function Actions({ id, role, onDone, onError }: { id: string; role: Role; onDone: () => void; onError: (value: string) => void }) {
  async function send(path: string, body: unknown) {
    try {
      await api(path, { method: path.includes("/api/gifts/") || path.includes("/api/tasks/") || path.includes("/api/packages/") || path.includes("/api/vip-plans/") || path.includes("/api/settings") || path.includes("/api/translations") ? "PUT" : "POST", body: JSON.stringify(body) });
      onError("");
      onDone();
    } catch (err) {
      onError(err instanceof Error ? err.message : "request failed");
    }
  }

  if (id === "users" && can(role, "moderator")) return <UserTools onSend={send} />;
  if (id === "reports" && can(role, "moderator")) return <ReportTools onSend={send} />;
  if (id === "appeals" && can(role, "moderator")) return <AppealTools onSend={send} />;
  if (id === "wallet" && can(role, "administrator")) return <WalletTools onSend={send} />;
  if (id === "referrals" && can(role, "administrator")) return <ReferralTools onSend={send} />;
  if (id === "vip" && can(role, "administrator")) return <VipTools onSend={send} />;
  if (id === "catalog" && can(role, "administrator")) return <CatalogTools onDone={onDone} onError={onError} />;
  if (id === "gifts" && can(role, "administrator")) return <GiftTools onSend={send} />;
  if (id === "tasks" && can(role, "administrator")) return <TaskTools onSend={send} />;
  if (id === "promos" && can(role, "administrator")) return <PromoTools onSend={send} />;
  if (id === "locales" && can(role, "administrator")) return <LocaleTools onSend={send} />;
  if (id === "settings" && can(role, "administrator")) return <SettingTools onSend={send} />;
  if (id === "admins" && role === "superadmin") return <AdminTools onSend={send} />;
  if (id === "catalog") return <CatalogTools onDone={onDone} onError={onError} readOnly />;
  return null;
}

function UserTools({ onSend }: { onSend: (path: string, body: unknown) => Promise<void> }) {
  const [userId, setUserId] = useState("");
  const [reason, setReason] = useState("нарушение правил");
  const [hours, setHours] = useState("24");
  const [status, setStatus] = useState("approved");
  return (
    <div className="row">
      <input aria-label="ID пользователя" value={userId} onChange={(event) => setUserId(event.target.value)} placeholder="ID" />
      <input aria-label="Причина" value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Причина" />
      <button type="button" onClick={() => onSend(`/api/users/${userId}/warn`, { reason })}>Предупредить</button>
      <input aria-label="Часы" value={hours} onChange={(event) => setHours(event.target.value)} placeholder="Часы" />
      <button type="button" onClick={() => onSend(`/api/users/${userId}/restrict`, { reason, hours: Number(hours) })}>Ограничить</button>
      <button type="button" onClick={() => onSend(`/api/users/${userId}/ban`, { reason })}>Заблокировать</button>
      <button type="button" onClick={() => onSend(`/api/users/${userId}/unban`, { reason })}>Снять блок</button>
      <select aria-label="Статус верификации" value={status} onChange={(event) => setStatus(event.target.value)}>
        {["approved", "rejected", "suspended", "revoked"].map((item) => <option key={item}>{item}</option>)}
      </select>
      <button type="button" onClick={() => onSend(`/api/users/${userId}/verification`, { status, reason })}>Статус проверки</button>
    </div>
  );
}

function ReportTools({ onSend }: { onSend: (path: string, body: unknown) => Promise<void> }) {
  const [id, setId] = useState("");
  const [resolution, setResolution] = useState("рассмотрено");
  const [action, setAction] = useState("dismiss");
  return (
    <div className="row">
      <input aria-label="ID жалобы" value={id} onChange={(event) => setId(event.target.value)} placeholder="ID жалобы" />
      <select aria-label="Действие" value={action} onChange={(event) => setAction(event.target.value)}>
        <option value="dismiss">отклонить</option>
        <option value="warn">предупреждение</option>
        <option value="restrict">ограничение 24ч</option>
        <option value="ban">блокировка</option>
      </select>
      <input aria-label="Решение" value={resolution} onChange={(event) => setResolution(event.target.value)} />
      <button type="button" onClick={() => onSend(`/api/reports/${id}/resolve`, { action, resolution })}>Закрыть жалобу</button>
    </div>
  );
}

function AppealTools({ onSend }: { onSend: (path: string, body: unknown) => Promise<void> }) {
  const [id, setId] = useState("");
  return (
    <div className="row">
      <input aria-label="ID обжалования" value={id} onChange={(event) => setId(event.target.value)} placeholder="ID" />
      <button type="button" onClick={() => onSend(`/api/appeals/${id}/resolve`, { action: "accept", resolution: "ограничение снято" })}>Принять</button>
      <button type="button" onClick={() => onSend(`/api/appeals/${id}/resolve`, { action: "dismiss", resolution: "оставлено в силе" })}>Отклонить</button>
    </div>
  );
}

function WalletTools({ onSend }: { onSend: (path: string, body: unknown) => Promise<void> }) {
  const [userId, setUserId] = useState("");
  const [amount, setAmount] = useState("10");
  const [reason, setReason] = useState("корректировка");
  const [key, setKey] = useState(`adjust-${Date.now()}`);
  return (
    <div className="row">
      <input aria-label="ID пользователя" value={userId} onChange={(event) => setUserId(event.target.value)} placeholder="ID" />
      <input aria-label="Сумма" value={amount} onChange={(event) => setAmount(event.target.value)} placeholder="Сумма, минус для списания" />
      <input aria-label="Причина" value={reason} onChange={(event) => setReason(event.target.value)} />
      <input aria-label="Ключ идемпотентности" value={key} onChange={(event) => setKey(event.target.value)} />
      <button type="button" onClick={() => onSend("/api/wallet/adjust", { user_id: Number(userId), amount: Number(amount), reason, idempotency_key: key })}>Провести операцию</button>
    </div>
  );
}

function ReferralTools({ onSend }: { onSend: (path: string, body: unknown) => Promise<void> }) {
  const [id, setId] = useState("");
  return (
    <div className="row">
      <input aria-label="ID реферала" value={id} onChange={(event) => setId(event.target.value)} placeholder="ID" />
      <button type="button" onClick={() => onSend(`/api/referrals/${id}/freeze`, { reason: "подозрительная регистрация" })}>Заморозить бонус</button>
    </div>
  );
}

function VipTools({ onSend }: { onSend: (path: string, body: unknown) => Promise<void> }) {
  const [userId, setUserId] = useState("");
  const [days, setDays] = useState("7");
  return (
    <div className="row">
      <input aria-label="ID пользователя" value={userId} onChange={(event) => setUserId(event.target.value)} placeholder="ID" />
      <input aria-label="Дни" value={days} onChange={(event) => setDays(event.target.value)} />
      <button type="button" onClick={() => onSend("/api/subscriptions/grant", { user_id: Number(userId), days: Number(days), reason: "выдано администратором" })}>Выдать VIP</button>
    </div>
  );
}

function CatalogTools({ onDone, onError, readOnly = false }: { onDone: () => void; onError: (value: string) => void; readOnly?: boolean }) {
  const [packages, setPackages] = useState<Row[]>([]);
  const [plans, setPlans] = useState<Row[]>([]);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    api<Row[]>("/api/packages").then(setPackages).catch((err: Error) => onError(err.message));
    api<Row[]>("/api/vip-plans").then(setPlans).catch((err: Error) => onError(err.message));
  }, [onError, tick]);
  function saved() {
    setTick((value) => value + 1);
    onDone();
  }
  return (
    <div>
      <h3>Пакеты LoveCoins</h3>
      <Table rows={packages} />
      {!readOnly && packages.map((row) => (
        <PackageEditor key={String(row.id)} row={row} onDone={saved} onError={onError} />
      ))}
      <h3>Планы VIP</h3>
      <Table rows={plans} />
      {!readOnly && plans.map((row) => (
        <PlanEditor key={String(row.id)} row={row} onDone={saved} onError={onError} />
      ))}
    </div>
  );
}

function PackageEditor({ row, onDone, onError }: { row: Row; onDone: () => void; onError: (value: string) => void }) {
  const [coins, setCoins] = useState(String(row.coins));
  const [stars, setStars] = useState(String(row.stars));
  const [active, setActive] = useState(Boolean(row.active));
  return (
    <div className="row">
      <span>{String(row.code)}</span>
      <input aria-label="Монеты" value={coins} onChange={(event) => setCoins(event.target.value)} />
      <input aria-label="Stars" value={stars} onChange={(event) => setStars(event.target.value)} />
      <label><input type="checkbox" checked={active} onChange={(event) => setActive(event.target.checked)} /> активен</label>
      <button type="button" onClick={() => api(`/api/packages/${row.id}`, { method: "PUT", body: JSON.stringify({ coins: Number(coins), stars: Number(stars), active }) }).then(onDone).catch((err: Error) => onError(err.message))}>Сохранить</button>
    </div>
  );
}

function PlanEditor({ row, onDone, onError }: { row: Row; onDone: () => void; onError: (value: string) => void }) {
  const [days, setDays] = useState(String(row.days));
  const [coins, setCoins] = useState(String(row.coin_price));
  const [stars, setStars] = useState(String(row.star_price));
  const [active, setActive] = useState(Boolean(row.active));
  return (
    <div className="row">
      <span>{String(row.code)}</span>
      <input aria-label="Дни" value={days} onChange={(event) => setDays(event.target.value)} />
      <input aria-label="Цена в монетах" value={coins} onChange={(event) => setCoins(event.target.value)} />
      <input aria-label="Цена в Stars" value={stars} onChange={(event) => setStars(event.target.value)} />
      <label><input type="checkbox" checked={active} onChange={(event) => setActive(event.target.checked)} /> активен</label>
      <button type="button" onClick={() => api(`/api/vip-plans/${row.id}`, { method: "PUT", body: JSON.stringify({ days: Number(days), coin_price: Number(coins), star_price: Number(stars), active }) }).then(onDone).catch((err: Error) => onError(err.message))}>Сохранить</button>
    </div>
  );
}

function GiftTools({ onSend }: { onSend: (path: string, body: unknown) => Promise<void> }) {
  const [id, setId] = useState("");
  const [price, setPrice] = useState("10");
  return (
    <div className="row">
      <input aria-label="ID подарка" value={id} onChange={(event) => setId(event.target.value)} placeholder="ID" />
      <input aria-label="Цена" value={price} onChange={(event) => setPrice(event.target.value)} />
      <button type="button" onClick={() => onSend(`/api/gifts/${id}`, { price: Number(price), active: true })}>Обновить цену</button>
    </div>
  );
}

function TaskTools({ onSend }: { onSend: (path: string, body: unknown) => Promise<void> }) {
  const [id, setId] = useState("");
  const [reward, setReward] = useState("10");
  return (
    <div className="row">
      <input aria-label="ID задания" value={id} onChange={(event) => setId(event.target.value)} placeholder="ID" />
      <input aria-label="Награда" value={reward} onChange={(event) => setReward(event.target.value)} />
      <button type="button" onClick={() => onSend(`/api/tasks/${id}`, { reward: Number(reward), active: true })}>Обновить награду</button>
    </div>
  );
}

function PromoTools({ onSend }: { onSend: (path: string, body: unknown) => Promise<void> }) {
  const [code, setCode] = useState("WELCOME");
  const [coins, setCoins] = useState("50");
  const [max, setMax] = useState("100");
  const [id, setId] = useState("");
  return (
    <div className="row">
      <input aria-label="Код" value={code} onChange={(event) => setCode(event.target.value)} />
      <input aria-label="Монеты" value={coins} onChange={(event) => setCoins(event.target.value)} />
      <input aria-label="Лимит" value={max} onChange={(event) => setMax(event.target.value)} />
      <button type="button" onClick={() => onSend("/api/promos", { code, coins: Number(coins), max_redemptions: Number(max), days_valid: 30 })}>Создать</button>
      <input aria-label="ID промокода" value={id} onChange={(event) => setId(event.target.value)} placeholder="ID для отключения" />
      <button type="button" onClick={() => onSend(`/api/promos/${id}/deactivate`, {})}>Отключить</button>
    </div>
  );
}

function LocaleTools({ onSend }: { onSend: (path: string, body: unknown) => Promise<void> }) {
  const [language, setLanguage] = useState("ru");
  const [key, setKey] = useState("menu.search");
  const [value, setValue] = useState("");
  return (
    <div className="row">
      <select aria-label="Язык" value={language} onChange={(event) => setLanguage(event.target.value)}>
        <option value="ru">ru</option>
        <option value="hy">hy</option>
        <option value="en">en</option>
      </select>
      <input aria-label="Ключ" value={key} onChange={(event) => setKey(event.target.value)} />
      <input aria-label="Текст" value={value} onChange={(event) => setValue(event.target.value)} placeholder="Новый текст" />
      <button type="button" onClick={() => onSend("/api/translations", { language, key, value })}>Сохранить перевод</button>
    </div>
  );
}

function SettingTools({ onSend }: { onSend: (path: string, body: unknown) => Promise<void> }) {
  const [key, setKey] = useState("daily_bonus");
  const [value, setValue] = useState("20");
  return (
    <div className="row">
      <input aria-label="Ключ настройки" value={key} onChange={(event) => setKey(event.target.value)} />
      <input aria-label="Значение" value={value} onChange={(event) => setValue(event.target.value)} />
      <button type="button" onClick={() => onSend("/api/settings", { key, value: Number(value) })}>Сохранить</button>
    </div>
  );
}

function VerificationQueue({ onError }: { onError: (value: string) => void }) {
  const [rows, setRows] = useState<Row[]>([]);
  const [media, setMedia] = useState<Record<string, string>>({});

  async function reload() {
    const data = await api<Row[]>("/api/verifications?status=in_review");
    setRows(data);
    const next: Record<string, string> = {};
    for (const row of data) {
      const id = String(row.id);
      if (row.has_photo) next[`${id}-photo`] = await loadMedia(id, "photo");
      if (row.has_video_note) next[`${id}-video`] = await loadMedia(id, "video");
    }
    setMedia(next);
  }

  useEffect(() => {
    reload().catch((err: Error) => onError(err.message));
  }, [onError]);

  async function decide(userId: unknown, status: "approved" | "rejected") {
    const reason = status === "approved" ? "фото и кружок проверены" : "материалы не подходят";
    await api(`/api/users/${userId}/verification`, {
      method: "POST",
      body: JSON.stringify({ status, reason }),
    });
    onError("");
    await reload();
  }

  if (!rows.length) return <p className="muted">Заявок на проверке нет.</p>;
  return (
    <div>
      {rows.map((row) => {
        const id = String(row.id);
        return (
          <article className="card" key={id} style={{ marginBottom: 12 }}>
            <p>Заявка {id} · пользователь {String(row.user_id)}</p>
            {media[`${id}-photo`] && <img alt="Фото для проверки" src={media[`${id}-photo`]} style={{ maxWidth: 240, borderRadius: 12 }} />}
            {media[`${id}-video`] && <video controls src={media[`${id}-video`]} style={{ maxWidth: 240, display: "block", marginTop: 8 }} />}
            <div className="row">
              <button type="button" onClick={() => decide(row.user_id, "approved").catch((err: Error) => onError(err.message))}>Подтвердить</button>
              <button type="button" onClick={() => decide(row.user_id, "rejected").catch((err: Error) => onError(err.message))}>Отклонить</button>
            </div>
          </article>
        );
      })}
    </div>
  );
}

async function loadMedia(id: string, kind: string): Promise<string> {
  const response = await fetch(apiUrl(`/api/verifications/${id}/media?kind=${kind}`), {
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  if (!response.ok) return "";
  return URL.createObjectURL(await response.blob());
}

function AdminTools({ onSend }: { onSend: (path: string, body: unknown) => Promise<void> }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("moderator");
  return (
    <div className="row">
      <input aria-label="Логин сотрудника" value={username} onChange={(event) => setUsername(event.target.value)} placeholder="Логин" />
      <input aria-label="Пароль сотрудника" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Пароль от 10 символов" />
      <select aria-label="Роль" value={role} onChange={(event) => setRole(event.target.value)}>
        <option>support</option>
        <option>moderator</option>
        <option>administrator</option>
      </select>
      <button type="button" onClick={() => onSend("/api/admins", { username, password, role })}>Создать</button>
    </div>
  );
}
