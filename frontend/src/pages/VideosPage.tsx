import { Navigate } from "react-router";

export function VideosPage() {
  return <Navigate to="/shots?stage=videos" replace />;
}
