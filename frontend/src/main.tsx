import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router";
import { Toaster } from "sonner";
import { App } from "./App";
import { ChapterProvider } from "./hooks/use-chapter";
import { useTheme } from "./hooks/use-theme";
import { ProjectProvider } from "./hooks/use-project";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
    mutations: {
      retry: 0,
    },
  },
});

/** 跟随应用明暗主题的全局提示。 */
function ThemedToaster() {
  const { dark } = useTheme();
  return (
    <Toaster
      position="bottom-right"
      theme={dark ? "dark" : "light"}
      richColors
      closeButton
      toastOptions={{ duration: 4_000 }}
    />
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <ProjectProvider>
          <ChapterProvider>
          <App />
          <ThemedToaster />
          </ChapterProvider>
        </ProjectProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
