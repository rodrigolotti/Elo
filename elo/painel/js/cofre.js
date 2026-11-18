// Cofre local: guarda no IndexedDB o par de chaves de cada organização.
//
// O IndexedDB guarda o próprio objeto CryptoKey (clone estruturado), então a
// chave privada continua não exportável depois de guardada: o que fica no
// disco não pode ser lido como bytes por este código nem por outro script
// da página.
//
// ATENÇÃO: limpar os dados do site apaga a chave, e sem ela a organização não
// assina mais nada. A recuperação vem na próxima etapa.

const BANCO = "elo-painel";
const VERSAO = 1;
const LOJA = "chaves";

function abrir() {
  return new Promise((resolve, reject) => {
    const pedido = indexedDB.open(BANCO, VERSAO);
    pedido.onupgradeneeded = () => {
      pedido.result.createObjectStore(LOJA, { keyPath: "cnpj" });
    };
    pedido.onsuccess = () => resolve(pedido.result);
    pedido.onerror = () => reject(pedido.error);
  });
}

async function transacao(modo, operacao) {
  const banco = await abrir();
  try {
    return await new Promise((resolve, reject) => {
      const tx = banco.transaction(LOJA, modo);
      const pedido = operacao(tx.objectStore(LOJA));
      tx.oncomplete = () => resolve(pedido.result);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  } finally {
    banco.close();
  }
}

/** Pede ao navegador para não apagar estes dados sozinho quando faltar espaço. */
export async function pedirArmazenamentoPersistente() {
  if (!navigator.storage?.persist) return false;
  return (await navigator.storage.persisted()) || navigator.storage.persist();
}

/**
 * Guarda a chave de uma organização. Recusa sobrescrever: perder a chave de
 * uma organização já cadastrada é irreversível nesta versão.
 */
export function guardarChave({ cnpj, razaoSocial, chaveId, par, chavePublica }) {
  return transacao("readwrite", (loja) => loja.add({
    cnpj, razaoSocial, chaveId, chavePublica,
    privada: par.privateKey, publica: par.publicKey,
    criadaEm: new Date().toISOString(),
  }));
}

/** Registro guardado de uma organização, ou undefined. */
export function lerChave(cnpj) {
  return transacao("readonly", (loja) => loja.get(cnpj));
}

/** Todas as organizações com chave neste navegador. */
export function listarChaves() {
  return transacao("readonly", (loja) => loja.getAll());
}

/** Remove a chave (usado se o cadastro no servidor falhar logo após gerar). */
export function apagarChave(cnpj) {
  return transacao("readwrite", (loja) => loja.delete(cnpj));
}
