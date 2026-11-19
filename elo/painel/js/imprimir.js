// Página /verificar: o botão "Imprimir" abre a impressão do navegador. Fica num
// arquivo (e não num onclick) para a página rodar com CSP sem script inline.

document.querySelector(".imprimir")?.addEventListener("click", () => window.print());
