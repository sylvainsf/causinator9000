export interface Config {
  c9k?: {
    /**
     * Base URL of the Causinator 9000 engine REST API.
     * @default "http://localhost:8080"
     */
    baseUrl?: string;
  };
}
