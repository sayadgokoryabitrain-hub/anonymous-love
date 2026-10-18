import { FormEvent, useEffect, useState } from "react";

type Overview = {
  id: number;
  nickname: string | null;
  gender: string | null;
  age: number | null;
  verification_status: string;
  trust_score: number;
  balance: number;
  gifts: { code: string; emoji: string; price: number; vip_only: boolean }[];
  tasks: { code: string; reward: number }[];
};

export function App() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [message, setMessage] = useState("Открой приложение из бота ANONYMOUS LOVE. Бот работает и без этого экрана.");
  const [code, setCode] = useState("");
  const [token, setToken] = useState("");

  useEffect(() => {
    const webApp = window.Telegram?.WebApp;
    webApp?.ready();
    webApp?.expand?.();
    const initData = webApp?.initData || "";
    if (!initData) return;
    void (async () => {
      const session = await fetch("/api/miniapp/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ init_data: initData }),
      });
      if (!session.ok) {
        setMessage("Сначала заверши регистрацию в боте.");
        return;
      }
      const data = (await session.json()) as { access_token: string };
      setToken(data.access_token);
      const me = await fetch("/api/miniapp/overview", {
        headers: { Authorization: `Bearer ${data.access_token}` },
      });
      if (!me.ok) {
        setMessage("Не удалось загрузить профиль.");
        return;
      }
      setOverview((await me.json()) as Overview);
      setMessage("");
    })();
  }, []);

  async function promo(event: FormEvent) {
    event.preventDefault();
    const response = await fetch("/api/miniapp/promo", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ code }),
    });
    const body = (await response.json()) as { coins?: number; detail?: string };
    setMessage(response.ok ? `Начислено ${body.coins} LoveCoins` : String(body.detail || "Промокод не принят"));
    if (response.ok && overview) setOverview({ ...overview, balance: overview.balance + (body.coins || 0) });
  }

  async function claim(task: string) {
    const response = await fetch(`/api/miniapp/tasks/${encodeURIComponent(task)}/claim`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
    });
    const body = (await response.json()) as { coins?: number; detail?: string };
    setMessage(response.ok ? `Задание выполнено: +${body.coins}` : String(body.detail || "Задание ещё не выполнено"));
    if (response.ok && overview) setOverview({ ...overview, balance: overview.balance + (body.coins || 0) });
  }

  if (!overview) {
    return (
      <main>
        <div className="card">
          <h1>ANONYMOUS LOVE</h1>
          <p className="muted">{message}</p>
        </div>
      </main>
    );
  }

  return (
    <main>
      <div className="card">
        <h1>{overview.nickname || "Анкета"}</h1>
        <p className="muted">
          {overview.gender} · {overview.age} · проверка: {overview.verification_status}
        </p>
        <p>Доверие: {overview.trust_score}</p>
        <p>LoveCoins: {overview.balance}</p>
      </div>
      <div className="card">
        <h2>Подарки</h2>
        <p className="muted">Отправка подарка остаётся в анонимном чате бота и требует активного разговора.</p>
        {overview.gifts.map((gift) => (
          <div className="row" key={gift.code}>
            <span>{gift.emoji} {gift.code}{gift.vip_only ? " · VIP" : ""}</span>
            <span>{gift.price}</span>
          </div>
        ))}
      </div>
      <div className="card">
        <h2>Задания</h2>
        {overview.tasks.map((task) => (
          <div className="row" key={task.code}>
            <span>{task.code} · {task.reward}</span>
            <button type="button" onClick={() => void claim(task.code)}>Забрать</button>
          </div>
        ))}
      </div>
      <form className="card" onSubmit={(event) => void promo(event)}>
        <h2>Промокод</h2>
        <input aria-label="Промокод" value={code} onChange={(event) => setCode(event.target.value)} />
        <button type="submit">Активировать</button>
        {message && <p>{message}</p>}
      </form>
    </main>
  );
}
