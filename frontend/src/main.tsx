import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import "@fontsource-variable/manrope";
import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { Toaster } from "sonner";
import { App } from "./App";
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
          <App />
          <Toaster
            position="bottom-right"
            richColors
            closeButton
            toastOptions={{ duration: 4_000 }}
          />
        </ProjectProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
