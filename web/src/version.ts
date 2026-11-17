// The version of TeXLoom this web app was built from (pyproject.toml, through vite.config.ts), sent
// to servers so that one can ask older apps to update (texloom-server --min-version).
declare const __TEXLOOM_VERSION__: string
export const APP_VERSION: string = typeof __TEXLOOM_VERSION__ === 'string' ? __TEXLOOM_VERSION__ : 'dev'
/** The X-TeXLoom header of requests to a server: which app, which version. */
export const APP_HEADER = { 'X-TeXLoom': `web/${APP_VERSION}` }
