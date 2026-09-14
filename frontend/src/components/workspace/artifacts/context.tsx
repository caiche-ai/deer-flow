import {
  createContext,
  useCallback,
  useContext,
  useState,
  type ReactNode,
} from "react";

import { useSidebar } from "@/components/ui/sidebar";
import { env } from "@/env";

export interface ArtifactsContextType {
  artifacts: string[];
  setArtifacts: (artifacts: string[]) => void;

  selectedArtifact: string | null;
  selectedDocument: DocumentPreviewSelection | null;
  autoSelect: boolean;
  select: (artifact: string, autoSelect?: boolean) => void;
  previewDocument: (document: DocumentPreviewSelection) => void;
  showArtifacts: () => void;
  deselect: () => void;

  open: boolean;
  autoOpen: boolean;
  setOpen: (open: boolean) => void;
}

export interface DocumentPreviewSelection {
  filename: string;
  path?: string;
  previewPath?: string;
  url?: string;
  page?: number;
  quote?: string;
  section?: string;
  clause?: string;
}

const ArtifactsContext = createContext<ArtifactsContextType | undefined>(
  undefined,
);

interface ArtifactsProviderProps {
  children: ReactNode;
}

export function ArtifactsProvider({ children }: ArtifactsProviderProps) {
  const [artifacts, setArtifacts] = useState<string[]>([]);
  const [selectedArtifact, setSelectedArtifact] = useState<string | null>(null);
  const [selectedDocument, setSelectedDocument] =
    useState<DocumentPreviewSelection | null>(null);
  const [autoSelect, setAutoSelect] = useState(true);
  const [open, setOpen] = useState(
    env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY === "true",
  );
  const [autoOpen, setAutoOpen] = useState(true);
  const { setOpen: setSidebarOpen } = useSidebar();

  const select = useCallback(
    (artifact: string, autoSelect = false) => {
      setSelectedArtifact(artifact);
      setSelectedDocument(null);
      if (env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY !== "true") {
        setSidebarOpen(false);
      }
      if (!autoSelect) {
        setAutoSelect(false);
      }
    },
    [setSidebarOpen, setSelectedArtifact, setAutoSelect],
  );

  const previewDocument = useCallback(
    (document: DocumentPreviewSelection) => {
      setSelectedDocument(document);
      setSelectedArtifact(null);
      setOpen(true);
      if (env.NEXT_PUBLIC_STATIC_WEBSITE_ONLY !== "true") {
        setSidebarOpen(false);
      }
    },
    [setSidebarOpen],
  );

  const showArtifacts = useCallback(() => {
    setSelectedDocument(null);
    setOpen(true);
  }, []);

  const deselect = useCallback(() => {
    setSelectedArtifact(null);
    setSelectedDocument(null);
    setAutoSelect(true);
    setOpen(false);
  }, []);

  const value: ArtifactsContextType = {
    artifacts,
    setArtifacts,

    open,
    autoOpen,
    autoSelect,
    setOpen: (isOpen: boolean) => {
      if (!isOpen && autoOpen) {
        setAutoOpen(false);
        setAutoSelect(false);
      }
      setOpen(isOpen);
    },

    selectedArtifact,
    selectedDocument,
    select,
    previewDocument,
    showArtifacts,
    deselect,
  };

  return (
    <ArtifactsContext.Provider value={value}>
      {children}
    </ArtifactsContext.Provider>
  );
}

export function useArtifacts() {
  const context = useContext(ArtifactsContext);
  if (context === undefined) {
    throw new Error("useArtifacts must be used within an ArtifactsProvider");
  }
  return context;
}
