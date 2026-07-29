import { useNavigate } from "react-router-dom";
import { useProject } from "../hooks/use-project";
import { EmptyState, LoadingState } from "./status";
import { Button } from "./ui";

export function ProjectRequired({ children }: { children: React.ReactNode }) {
  const navigate = useNavigate();
  const { activeProject, isLoading } = useProject();
  if (isLoading) return <LoadingState label="正在读取项目…" />;
  if (!activeProject) {
    return (
      <EmptyState
        title="先创建一个漫剧项目"
        description="生产轨道中的内容都归属于项目。录入小说和统一画风后，就可以开始分析与生成。"
        action={
          <Button variant="primary" onClick={() => navigate("/project?create=1")}>
            创建第一个项目
          </Button>
        }
      />
    );
  }
  return children;
}
