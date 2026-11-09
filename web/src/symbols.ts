// The symbol palette's contents: pairs of what the button shows and its LaTeX. A line starting
// @package lists symbols that need that package. `{}` marks an argument.
export interface SymbolGroup { id: string; text?: boolean; symbols: Symbol[] }
export interface Symbol { glyph: string; latex: string; pkg?: string }

const parse = (lines: string): Symbol[] => lines.trim().split('\n').flatMap((line) => {
  const [pkg, ...items] = line.trim().startsWith('@') ? line.trim().slice(1).split(/\s+/) : ['', ...line.trim().split(/\s+/)]
  const out: Symbol[] = []
  for (let i = 0; i + 1 < items.length; i += 2) out.push({ glyph: items[i], latex: items[i + 1], ...(pkg ? { pkg } : {}) })
  return out
})

export const SYMBOLS: SymbolGroup[] = [
  { id: 'greek', symbols: parse(String.raw`
    α \alpha β \beta γ \gamma δ \delta ϵ \epsilon ε \varepsilon ζ \zeta η \eta θ \theta ϑ \vartheta ι \iota κ \kappa
    λ \lambda μ \mu ν \nu ξ \xi π \pi ϖ \varpi ρ \rho ϱ \varrho σ \sigma ς \varsigma τ \tau υ \upsilon ϕ \phi φ \varphi
    χ \chi ψ \psi ω \omega Γ \Gamma Δ \Delta Θ \Theta Λ \Lambda Ξ \Xi Π \Pi Σ \Sigma Υ \Upsilon Φ \Phi Ψ \Psi Ω \Omega
    @amssymb ϝ \digamma ϰ \varkappa
  `) },
  { id: 'relations', symbols: parse(String.raw`
    ≤ \leq ≥ \geq ≠ \neq ≡ \equiv ≈ \approx ≅ \cong ≃ \simeq ∼ \sim ∝ \propto ≪ \ll ≫ \gg ≺ \prec ≻ \succ ⪯ \preceq ⪰ \succeq
    ∈ \in ∉ \notin ∋ \ni ⊂ \subset ⊃ \supset ⊆ \subseteq ⊇ \supseteq ⊥ \perp ∥ \parallel ∣ \mid ⊢ \vdash ⊣ \dashv ⊨ \models ≐ \doteq ≍ \asymp ⋈ \bowtie
    @amssymb ⩽ \leqslant ⩾ \geqslant ≲ \lesssim ≳ \gtrsim ≁ \nsim ≇ \ncong ≰ \nleq ≱ \ngeq ⊈ \nsubseteq ⊉ \nsupseteq ⊊ \subsetneq ⊋ \supsetneq ≜ \triangleq ∦ \nparallel ∤ \nmid ⊬ \nvdash ⊭ \nvDash ⊩ \Vdash ⊨ \vDash ∴ \therefore ∵ \because
    @mathtools ≔ \coloneqq ≕ \eqqcolon
  `) },
  { id: 'operators', symbols: parse(String.raw`
    ± \pm ∓ \mp × \times ÷ \div · \cdot ∘ \circ ∙ \bullet ∗ \ast ⋆ \star ∪ \cup ∩ \cap ⊎ \uplus ⊓ \sqcap ⊔ \sqcup ∨ \vee ∧ \wedge
    ⊕ \oplus ⊖ \ominus ⊗ \otimes ⊘ \oslash ⊙ \odot † \dagger ‡ \ddagger ≀ \wr ⨿ \amalg ◃ \triangleleft ▹ \triangleright △ \bigtriangleup ▽ \bigtriangledown ∖ \setminus
    @amssymb ⋉ \ltimes ⋊ \rtimes ⊠ \boxtimes ⊞ \boxplus ⊡ \boxdot ⊟ \boxminus ⋓ \Cup ⋒ \Cap ⊼ \barwedge ⊻ \veebar
  `) },
  { id: 'large', symbols: parse(String.raw`
    ∑ \sum ∏ \prod ∐ \coprod ∫ \int ∮ \oint ⋃ \bigcup ⋂ \bigcap ⨆ \bigsqcup ⋁ \bigvee ⋀ \bigwedge ⨁ \bigoplus ⨂ \bigotimes ⨀ \bigodot ⨄ \biguplus
    @amsmath ∬ \iint ∭ \iiint ⨌ \iiiint
  `) },
  { id: 'arrows', symbols: parse(String.raw`
    ← \leftarrow → \rightarrow ↔ \leftrightarrow ⇐ \Leftarrow ⇒ \Rightarrow ⇔ \Leftrightarrow ↑ \uparrow ↓ \downarrow ↕ \updownarrow ⇑ \Uparrow ⇓ \Downarrow ⇕ \Updownarrow
    ↦ \mapsto ⟼ \longmapsto ⟵ \longleftarrow ⟶ \longrightarrow ⟷ \longleftrightarrow ⟸ \Longleftarrow ⟹ \Longrightarrow ⟺ \Longleftrightarrow ↪ \hookrightarrow ↩ \hookleftarrow
    ↗ \nearrow ↘ \searrow ↙ \swarrow ↖ \nwarrow ⇀ \rightharpoonup ↽ \leftharpoondown ⇌ \rightleftharpoons
    @amssymb ⇝ \rightsquigarrow ⇢ \dashrightarrow ⇠ \dashleftarrow ↠ \twoheadrightarrow ↞ \twoheadleftarrow ↣ \rightarrowtail ↢ \leftarrowtail ⇉ \rightrightarrows ⇇ \leftleftarrows ⇆ \leftrightarrows ⇄ \rightleftarrows ↛ \nrightarrow ↚ \nleftarrow ⇏ \nRightarrow ⇍ \nLeftarrow ↺ \circlearrowleft ↻ \circlearrowright
  `) },
  { id: 'delimiters', symbols: parse(String.raw`
    ⟨ \langle ⟩ \rangle ⌈ \lceil ⌉ \rceil ⌊ \lfloor ⌋ \rfloor ‖ \| { \{ } \}
    @amsmath | \lvert | \rvert ‖ \lVert ‖ \rVert
    @stmaryrd ⟦ \llbracket ⟧ \rrbracket
  `) },
  { id: 'misc', symbols: parse(String.raw`
    ∀ \forall ∃ \exists ∅ \emptyset ¬ \neg ∂ \partial ∇ \nabla ∞ \infty ⊤ \top ⊥ \bot ℵ \aleph ℘ \wp ℜ \Re ℑ \Im ℏ \hbar ℓ \ell ı \imath ȷ \jmath ′ \prime ∠ \angle △ \triangle
    ♭ \flat ♮ \natural ♯ \sharp ♣ \clubsuit ♢ \diamondsuit ♡ \heartsuit ♠ \spadesuit √ \surd … \ldots ⋯ \cdots ⋮ \vdots ⋱ \ddots
    @amssymb ∄ \nexists ∅ \varnothing ∁ \complement □ \square ■ \blacksquare ◊ \lozenge ⧫ \blacklozenge ★ \bigstar ∡ \measuredangle ℧ \mho ð \eth ℶ \beth ℷ \gimel ℸ \daleth ✓ \checkmark ✠ \maltese
  `) },
  { id: 'letters', symbols: parse(String.raw`
    𝒜 \mathcal{A} ℬ \mathcal{B} 𝒞 \mathcal{C} ℱ \mathcal{F} ℋ \mathcal{H} ℒ \mathcal{L} 𝒪 \mathcal{O}
    @amssymb ℝ \mathbb{R} ℕ \mathbb{N} ℤ \mathbb{Z} ℚ \mathbb{Q} ℂ \mathbb{C} ℙ \mathbb{P} 𝔼 \mathbb{E} 𝔽 \mathbb{F} 𝔤 \mathfrak{g} 𝔄 \mathfrak{A}
    @dsfont 𝟙 \mathds{1}
    @mathrsfs 𝓐 \mathscr{A} 𝓛 \mathscr{L}
  `) },
  { id: 'structures', symbols: parse(String.raw`
    ½ \frac{}{} √x \sqrt{} x̂ \hat{} x̃ \tilde{} x̄ \bar{} x⃗ \vec{} ẋ \dot{} ẍ \ddot{} x̌ \check{} x́ \acute{} x̀ \grave{} x̆ \breve{}
    x̅ \overline{} x̲ \underline{} ⏞ \overbrace{} ⏟ \underbrace{} x̂̂ \widehat{} x̃̃ \widetilde{} R \mathrm{} B \mathbf{} I \mathit{} S \mathsf{} T \mathtt{}
    @amsmath x⃛ \dddot{} ⁿ⁄ₖ \binom{}{} ½ \dfrac{}{} ½ \tfrac{}{} β \boldsymbol{} Tx \text{}
    @bm β \bm{}
    @cancel x̸ \cancel{}
  `) },
  { id: 'text', text: true, symbols: parse(String.raw`
    § \S ¶ \P © \copyright £ \pounds † \dag ‡ \ddag • \textbullet … \dots – -- — --- % \% & \& # \# _ \_ $ \$ { \{ } \} \ \textbackslash ~ \textasciitilde ^ \textasciicircum
    ° \textdegree € \texteuro ™ \texttrademark ® \textregistered
  `) },
]
