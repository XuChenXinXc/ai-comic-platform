import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import "@fontsource-variable/manrope";
import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router";
import { Toaster } from "sonner";
import { App } from "./App";
import { ChapterProvider } from "./hooks/use-chapter";
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

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <ProjectProvider>
          <ChapterProvider>
          <App />
          <Toaster
            position="bottom-right"
            richColors
            closeButton
            toastOptions={{ duration: 4_000 }}
          />
          </ChapterProvider>
        </ProjectProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
