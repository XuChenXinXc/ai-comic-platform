import { Navigate, Route, Routes } from "react-router";
import { AppShell } from "./components/app-shell";
import { AssetImagesPage } from "./pages/AssetImagesPage";
import { AssetsPage } from "./pages/AssetsPage";
import { JobsPage } from "./pages/JobsPage";
import { KeyframesPage } from "./pages/KeyframesPage";
import { PlatformSettingsPage } from "./pages/PlatformSettingsPage";
import { ProjectPage } from "./pages/ProjectPage";
import { ProvidersPage } from "./pages/ProvidersPage";
import { ShotsPage } from "./pages/ShotsPage";
import { VideosPage } from "./pages/VideosPage";
import { WritingPage } from "./pages/WritingPage";

export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<Navigate to="/project" replace />} />
        <Route path="/project" element={<ProjectPage />} />
        <Route path="/writing" element={<WritingPage />} />
        <Route path="/assets" element={<AssetsPage />} />
        <Route path="/asset-images" element={<AssetImagesPage />} />
        <Route path="/shots" element={<ShotsPage />} />
        <Route path="/keyframes" element={<KeyframesPage />} />
        <Route path="/videos" element={<VideosPage />} />
        <Route path="/providers" element={<ProvidersPage />} />
        <Route path="/jobs" element={<JobsPage />} />
        <Route path="/settings" element={<PlatformSettingsPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/project" replace />} />
    </Routes>
  );
}
