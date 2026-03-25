-- CreateEnum
CREATE TYPE "TransactionKind" AS ENUM ('income', 'expense');

-- CreateEnum
CREATE TYPE "TransactionCategory" AS ENUM ('salario', 'freelance', 'investimentos', 'vendas', 'reembolso', 'bonus', 'outros_receitas', 'alimentacao', 'moradia', 'transporte', 'saude', 'educacao', 'lazer', 'impostos', 'assinaturas', 'contas', 'compras', 'outros_gastos');

-- CreateTable
CREATE TABLE "transactions" (
    "id" SERIAL NOT NULL,
    "kind" "TransactionKind" NOT NULL,
    "amount" DECIMAL(12,2) NOT NULL,
    "category" "TransactionCategory" NOT NULL,
    "description" TEXT NOT NULL DEFAULT '',
    "occurred_on" DATE NOT NULL,
    "created_at" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "transactions_pkey" PRIMARY KEY ("id")
);

-- CreateIndex
CREATE INDEX "idx_transactions_kind_occurred_on" ON "transactions"("kind", "occurred_on");

-- CreateIndex
CREATE INDEX "idx_transactions_occurred_on" ON "transactions"("occurred_on");
