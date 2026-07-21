import { BrowserRouter, Routes, Route } from "react-router-dom";
import { ThemeProvider } from "./contexts/ThemeContext";
import { AIProvider } from "./contexts/AIContext";
import Layout from "./components/Layout";
import HomePage from "./pages/HomePage";
import ChatPage from "./pages/ChatPage";
import MemoriesHubPage from "./pages/MemoriesHubPage";
import CheckInPage from "./pages/CheckInPage";
import MomentsPage from "./pages/MomentsPage";
import GroupChatPage from "./pages/GroupChatPage";
import ForumPage from "./pages/ForumPage";
import AiProfilesPage from "./pages/AiProfilesPage";
import PersonsPage from "./pages/PersonsPage";
import ThemePage from "./pages/ThemePage";
import SettingsPage from "./pages/SettingsPage";
import PulsePage from "./pages/PulsePage";
import ObservatoryPage from "./pages/ObservatoryPage";

import "./styles/theme.css";
import "./styles/layout.css";

export default function App() {
  return (
    <ThemeProvider>
      <AIProvider>
        <BrowserRouter basename="/app">
          <Routes>
            <Route element={<Layout />}>
              <Route index element={<HomePage />} />
              <Route path="chat" element={<ChatPage />} />
              <Route path="memories" element={<MemoriesHubPage />} />
              <Route path="timeline" element={<MemoriesHubPage initialView="timeline" />} />
              <Route path="pulse" element={<PulsePage />} />
              <Route path="observatory" element={<ObservatoryPage />} />
              <Route path="checkin" element={<CheckInPage />} />
              <Route path="moments" element={<MomentsPage />} />
              <Route path="group" element={<GroupChatPage />} />
              <Route path="forum" element={<ForumPage />} />
              <Route path="ai-profiles" element={<AiProfilesPage />} />
              <Route path="persons" element={<PersonsPage />} />
              <Route path="theme" element={<ThemePage />} />
              <Route path="settings" element={<SettingsPage />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </AIProvider>
    </ThemeProvider>
  );
}
