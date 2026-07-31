import { Navigate } from "react-router-dom";

export function VideosPage() {
  return <Navigate to="/shots?stage=videos" replace />;
}
