/// <reference types="vite/client" />

interface TelegramWebApp {
  ready: () => void;
  expand?: () => void;
  initData: string;
}

interface Window {
  Telegram?: { WebApp?: TelegramWebApp };
}
