import { BrowserRouter, Routes, Route } from "react-router-dom";
import { ThemeProvider } from "./contexts/ThemeContext";
import Layout from "./components/Layout";
import HomePage from "./pages/HomePage";
import ChatPage from "./pages/ChatPage";
import MemoriesPage from "./pages/MemoriesPage";
import CheckInPage from "./pages/CheckInPage";
import MomentsPage from "./pages/MomentsPage";
import GroupChatPage from "./pages/GroupChatPage";
import ForumPage from "./pages/ForumPage";
import AiProfilesPage from "./pages/AiProfilesPage";
import ThemePage from "./pages/ThemePage";
import SettingsPage from "./pages/SettingsPage";
import TimelinePage from "./pages/TimelinePage";

import "./styles/theme.css";
import "./styles/layout.css";

export default function App() {
  return (
    <ThemeProvider>
      <BrowserRouter basename="/app">
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<HomePage />} />
            <Route path="chat" element={<ChatPage />} />
            <Route path="memories" element={<MemoriesPage />} />
            <Route path="timeline" element={<TimelinePage />} />
            <Route path="checkin" element={<CheckInPage />} />
            <Route path="moments" element={<MomentsPage />} />
            <Route path="group" element={<GroupChatPage />} />
            <Route path="forum" element={<ForumPage />} />
            <Route path="ai-profiles" element={<AiProfilesPage />} />
            <Route path="theme" element={<ThemePage />} />
            <Route path="settings" element={<SettingsPage />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </ThemeProvider>
  );
}
