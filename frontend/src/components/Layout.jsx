import { NavLink, Outlet } from "react-router-dom";
import { Home, MessageCircle, Brain, Settings, Palette, CalendarCheck, Heart, Users, MessageSquare, Bot, HeartPulse, Gauge } from "lucide-react";

const NAV = [
  { to: "/observatory", icon: Gauge,       label: "观测台" },
  { to: "/",         icon: Home,           label: "首页" },
  { to: "/chat",     icon: MessageCircle,  label: "对话" },
  { to: "/memories", icon: Brain,          label: "记忆" },
  { to: "/pulse",    icon: HeartPulse,     label: "情绪" },
  { to: "/moments",  icon: Heart,          label: "朋友圈" },
  { to: "/group",    icon: Users,          label: "群聊" },
  { to: "/forum",    icon: MessageSquare,  label: "论坛" },
  { to: "/ai-profiles", icon: Bot,        label: "AI档案" },
  { to: "/checkin",  icon: CalendarCheck,  label: "打卡" },
  { to: "/theme",    icon: Palette,        label: "主题" },
  { to: "/settings", icon: Settings,       label: "设置" },
];

function NavItems({ className }) {
  return NAV.map(({ to, icon: Icon, label }) => (
    <NavLink
      key={to}
      to={to}
      end={to === "/"}
      className={({ isActive }) => `${className} ${isActive ? "active" : ""}`}
    >
      <Icon />
      <span>{label}</span>
    </NavLink>
  ));
}

export default function Layout() {
  return (
    <div className="app-layout">
      {/* PC Sidebar */}
      <aside className="sidebar">
        <div className="sidebar-brand">
          <span style={{ fontSize: 24 }}>🐱</span>
          <h1>Memory Hub</h1>
        </div>
        <nav>
          <NavItems className="nav-item" />
        </nav>
        <div className="sidebar-footer">
          <div style={{ fontSize: 12, color: "var(--text-muted)", padding: "0 12px" }}>
            小猫 & 小克的记忆之家
          </div>
        </div>
      </aside>

      {/* Main Content */}
      <main className="main-content">
        <Outlet />
      </main>

      {/* Mobile Bottom Nav */}
      <nav className="bottom-nav">
        <div className="bottom-nav-inner">
          <NavItems className="bottom-nav-item" />
        </div>
      </nav>
    </div>
  );
}
