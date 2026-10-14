import rareRabbitProduct from "./rareRabbitProduct.json" with { type: "json" };

function jsonParser(data) {
  return JSON.parse(data);
}

console.log(">>json", rareRabbitProduct.query);
