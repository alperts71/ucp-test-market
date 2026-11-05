#!/usr/bin/env python3
"""Generate app/products.json: 500 supermarket products with size variants.

Each product is one brand's version of a product type (e.g. "Sütaş Whole
Milk"); its sizes are variants. Prices are in kurus (1/100 Turkish lira).
The random seed is fixed, so running this again gives the same file.

  python scripts/generate_products.py
"""

import json
import random
import re
from pathlib import Path

random.seed(500)

# Real Turkish market brands, grouped by what they actually sell. This is a
# test store: nothing is sold and it has no connection to these brands.
B = {
  "milk": ["Sütaş", "Pınar", "Torku", "İçim", "Eker", "Teksüt", "Yörükoğlu", "Ekici", "Danone"],
  "plant": ["Alpro", "Pınar", "Sütaş"],
  "goat": ["Pınar", "Eker", "Sütaş"],
  "yogurt": ["Sütaş", "Pınar", "Torku", "İçim", "Eker", "Danone", "Yörükoğlu", "Teksüt", "Ekici"],
  "butter": ["Sütaş", "Pınar", "Torku", "İçim", "Eker", "Teksüt", "Yörükoğlu"],
  "cheese": ["Sütaş", "Pınar", "Torku", "İçim", "Eker", "Teksüt", "Ekici", "Muratbey", "Tahsildaroğlu", "Bahçıvan", "Kaanlar", "Yörükoğlu"],
  "olive": ["Marmarabirlik", "Tariş", "Komili", "Kristal", "Fora"],
  "honey": ["Balparmak", "Anzer Bal", "Seyidoğlu", "Koska", "Altınbaşak"],
  "jam": ["Tamek", "Seyidoğlu", "Koska", "Kenton", "Tat", "Altınbaşak"],
  "tahini": ["Koska", "Seyidoğlu", "Hasköy", "Altınbaşak", "Kabadayi"],
  "spread": ["Sarelle", "Nutella", "Ülker", "Fiskobirlik", "Koska"],
  "egg": ["Keskinoğlu", "Banvit", "Ovo", "Erpiliç", "Gezen Tavuk"],
  "meat": ["Namet", "Pınar", "Apikoğlu", "Cumhuriyet", "Polonez", "Kasap Döner"],
  "cereal": ["Nestlé", "Kellogg's", "Ülker", "Eti", "Torku"],
  "bread": ["Uno", "Ekmecik", "Untad", "Vakfıkebir"],
  "simit": ["Simit Sarayı", "Uno"],
  "tea": ["Çaykur", "Doğuş", "Lipton", "Ofçay", "Kurukahveci Mehmet Efendi"],
  "coffee": ["Kurukahveci Mehmet Efendi", "Kahve Dünyası", "Jacobs", "Nescafé", "Selamlique"],
  "juice": ["Cappy", "Dimes", "Meysu", "Tamek", "Pınar"],
}

# (category, brand group, English name, Turkish name, [(size, base price)])
# Base prices are in US cents and are converted to Turkish lira below.
TYPES = [
  ("milk", "milk", "Whole Milk", "tam yagli sut", [("500 ml", 89), ("1 L", 149), ("1.5 L", 209)]),
  ("milk", "milk", "Semi-Skimmed Milk", "yarim yagli sut", [("500 ml", 85), ("1 L", 145), ("1.5 L", 199)]),
  ("milk", "milk", "Skimmed Milk", "yagsiz sut", [("500 ml", 85), ("1 L", 145)]),
  ("milk", "milk", "Lactose-Free Milk", "laktozsuz sut", [("500 ml", 119), ("1 L", 199)]),
  ("milk", "milk", "Organic Milk", "organik sut", [("500 ml", 139), ("1 L", 239)]),
  ("milk", "milk", "Chocolate Milk", "kakaolu sut", [("200 ml", 59), ("500 ml", 119), ("1 L", 199)]),
  ("milk", "milk", "Strawberry Milk", "cilekli sut", [("200 ml", 59), ("500 ml", 119)]),
  ("milk", "milk", "Banana Milk", "muzlu sut", [("200 ml", 59), ("500 ml", 119)]),
  ("milk", "goat", "Goat Milk", "keci sutu", [("500 ml", 199), ("1 L", 349)]),
  ("milk", "plant", "Almond Drink", "badem sutu", [("1 L", 299)]),
  ("milk", "plant", "Oat Drink", "yulaf sutu", [("1 L", 279)]),
  ("dairy", "yogurt", "Ayran", "ayran", [("200 ml", 39), ("1 L", 129), ("2 L", 219)]),
  ("dairy", "yogurt", "Kefir", "kefir", [("250 ml", 99), ("1 L", 279)]),
  ("dairy", "yogurt", "Plain Yogurt", "sade yogurt", [("500 g", 129), ("1 kg", 229), ("2 kg", 399)]),
  ("dairy", "yogurt", "Strained Yogurt", "suzme yogurt", [("500 g", 219), ("1 kg", 389)]),
  ("dairy", "yogurt", "Fruit Yogurt", "meyveli yogurt", [("4 x 110 g", 199), ("500 g", 189)]),
  ("dairy", "butter", "Butter", "tereyagi", [("125 g", 249), ("250 g", 449), ("500 g", 849)]),
  ("dairy", "butter", "Clotted Cream", "kaymak", [("100 g", 249), ("200 g", 449)]),
  ("dairy", "butter", "Cooking Cream", "krema", [("200 ml", 129), ("1 L", 499)]),
  ("cheese", "cheese", "White Cheese", "beyaz peynir", [("250 g", 299), ("500 g", 549), ("1 kg", 999)]),
  ("cheese", "cheese", "Full-Fat White Cheese", "tam yagli beyaz peynir", [("400 g", 499), ("800 g", 949)]),
  ("cheese", "cheese", "Kashar Cheese", "kasar peyniri", [("200 g", 349), ("400 g", 649), ("700 g", 1099)]),
  ("cheese", "cheese", "Aged Kashar Cheese", "eski kasar", [("250 g", 499), ("500 g", 949)]),
  ("cheese", "cheese", "Tulum Cheese", "tulum peyniri", [("250 g", 449), ("500 g", 849)]),
  ("cheese", "cheese", "Labneh", "labne", [("180 g", 199), ("400 g", 399)]),
  ("cheese", "cheese", "Cream Cheese", "krem peynir", [("150 g", 179), ("300 g", 319)]),
  ("cheese", "cheese", "Cheddar", "cheddar peyniri", [("200 g", 349), ("400 g", 649)]),
  ("cheese", "cheese", "Mozzarella", "mozzarella", [("125 g", 199), ("250 g", 369)]),
  ("cheese", "cheese", "Goat Cheese", "keci peyniri", [("200 g", 549), ("400 g", 999)]),
  ("cheese", "cheese", "String Cheese", "dil peyniri", [("250 g", 399), ("500 g", 749)]),
  ("cheese", "cheese", "Curd Cheese", "lor peyniri", [("250 g", 129), ("500 g", 229)]),
  ("cheese", "cheese", "Halloumi", "hellim", [("200 g", 399), ("400 g", 749)]),
  ("breakfast", "olive", "Black Olives", "siyah zeytin", [("200 g", 249), ("400 g", 449), ("1 kg", 999)]),
  ("breakfast", "olive", "Green Olives", "yesil zeytin", [("200 g", 229), ("400 g", 419), ("1 kg", 949)]),
  ("breakfast", "honey", "Flower Honey", "cicek bali", [("225 g", 399), ("460 g", 749), ("850 g", 1299)]),
  ("breakfast", "honey", "Pine Honey", "cam bali", [("460 g", 899), ("850 g", 1599)]),
  ("breakfast", "jam", "Strawberry Jam", "cilek receli", [("380 g", 249), ("800 g", 449)]),
  ("breakfast", "jam", "Apricot Jam", "kayisi receli", [("380 g", 249), ("800 g", 449)]),
  ("breakfast", "jam", "Sour Cherry Jam", "visne receli", [("380 g", 269), ("800 g", 479)]),
  ("breakfast", "jam", "Rose Jam", "gul receli", [("380 g", 299)]),
  ("breakfast", "tahini", "Tahini", "tahin", [("300 g", 349), ("600 g", 649)]),
  ("breakfast", "tahini", "Grape Molasses", "uzum pekmezi", [("380 g", 249), ("800 g", 449)]),
  ("breakfast", "spread", "Hazelnut Cocoa Spread", "findik kremasi", [("350 g", 399), ("700 g", 699)]),
  ("breakfast", "spread", "Peanut Butter", "fistik ezmesi", [("340 g", 399), ("680 g", 699)]),
  ("breakfast", "egg", "Free-Range Eggs", "gezen tavuk yumurtasi", [("10 pcs", 349), ("15 pcs", 499), ("30 pcs", 949)]),
  ("breakfast", "egg", "Eggs", "yumurta", [("10 pcs", 249), ("15 pcs", 349), ("30 pcs", 649)]),
  ("breakfast", "meat", "Beef Sucuk", "dana sucuk", [("200 g", 549), ("400 g", 999)]),
  ("breakfast", "meat", "Pastirma", "pastirma", [("100 g", 649), ("200 g", 1199)]),
  ("breakfast", "cereal", "Corn Flakes", "misir gevregi", [("375 g", 299), ("750 g", 529)]),
  ("breakfast", "cereal", "Granola", "granola", [("400 g", 449), ("800 g", 799)]),
  ("breakfast", "cereal", "Rolled Oats", "yulaf ezmesi", [("500 g", 249), ("1 kg", 429)]),
  ("bakery", "bread", "White Bread", "beyaz ekmek", [("350 g", 36), ("700 g", 66)]),
  ("bakery", "bread", "Whole Wheat Bread", "tam bugday ekmegi", [("350 g", 52), ("700 g", 94)]),
  ("bakery", "simit", "Simit", "simit", [("4 pcs", 199), ("8 pcs", 369)]),
  ("drinks", "tea", "Black Tea", "siyah cay", [("500 g", 499), ("1 kg", 899)]),
  ("drinks", "tea", "Green Tea", "yesil cay", [("20 bags", 199), ("50 bags", 429)]),
  ("drinks", "coffee", "Turkish Coffee", "turk kahvesi", [("100 g", 249), ("250 g", 549)]),
  ("drinks", "coffee", "Filter Coffee", "filtre kahve", [("250 g", 599), ("500 g", 1099)]),
  ("drinks", "juice", "Orange Juice", "portakal suyu", [("200 ml", 79), ("1 L", 269)]),
  ("cheese", "cheese", "Light White Cheese", "light beyaz peynir", [("350 g", 449), ("700 g", 849)]),
  ("cheese", "cheese", "Shredded Kashar", "rende kasar", [("200 g", 369), ("500 g", 849)]),
  ("dairy", "yogurt", "Light Yogurt", "light yogurt", [("500 g", 139), ("1 kg", 249)]),
  ("dairy", "yogurt", "Drinkable Yogurt", "icilebilir yogurt", [("200 ml", 69), ("1 L", 219)]),
  ("breakfast", "olive", "Olive Paste", "zeytin ezmesi", [("180 g", 249), ("330 g", 399)]),
  ("breakfast", "olive", "Extra Virgin Olive Oil", "naturel sizma zeytinyagi", [("500 ml", 799), ("1 L", 1449)]),
  ("breakfast", "honey", "Comb Honey", "petek bal", [("400 g", 1099), ("800 g", 1999)]),
  ("breakfast", "jam", "Fig Jam", "incir receli", [("380 g", 279), ("800 g", 499)]),
]


_ASCII = str.maketrans("çğıöşüéÇĞİÖŞÜÉ'", "cgiosueCGIOSUE ")


def slug(text: str) -> str:
  return re.sub(r"[^a-z0-9]+", "-", text.translate(_ASCII).lower()).strip("-")


def main() -> None:
  combos = [(t, b) for t in TYPES for b in B[t[1]]]
  random.shuffle(combos)
  combos = sorted(combos[:500], key=lambda c: (TYPES.index(c[0]), B[c[0][1]].index(c[1])))

  products = []
  sku = 0
  for n, ((category, _group, name, tr_name, sizes), brand) in enumerate(combos, 1):
    factor = random.uniform(0.8, 1.4)
    # Most products come in every size; some brands skip one.
    chosen = sizes if len(sizes) < 3 or random.random() < 0.6 else random.sample(sizes, 2)
    chosen = [s for s in sizes if s in chosen]
    variants = []
    for label, base in chosen:
      sku += 1
      # About 48 TRY per USD, calibrated to Turkish shelf prices in late 2026.
      # Prices end in ,50 or ,90 like real shelf prices.
      lira = base * factor * 48 / 100
      price = int(lira) * 100 + (50 if lira % 1 < 0.5 else 90)
      stock = 0 if sku % 41 == 0 else random.choice([10, 20, 30, 50])
      variants.append({"id": f"sku_{sku:05d}", "size": label, "price": price, "inventory": stock})
    title = f"{brand} {name}"
    products.append(
      {
        "id": f"prod_{n:04d}",
        "handle": slug(title),
        "brand": brand,
        "title": title,
        "category": category,
        "description": f"{name} ({tr_name}) by {brand}. Test listing; this store is not affiliated with the brand.",
        "variants": variants,
      }
    )
  out = Path(__file__).resolve().parent.parent / "app" / "products.json"
  out.write_text(json.dumps(products, indent=1, ensure_ascii=False) + "\n")
  print(f"{len(products)} products, {sku} variants -> {out}")


if __name__ == "__main__":
  main()
