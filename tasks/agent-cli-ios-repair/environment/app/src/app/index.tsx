import { useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { cartTotalCents } from '@/cart';
import products from '@/products.json';

export default function Cart() {
  const [quantities, setQuantities] = useState<Record<string, number>>({ coffee: 2, tea: 1 });
  const heading = { title: 'Coffee cart' };
  const total = cartTotalCents(products, quantities);

  return (
    <View style={styles.screen}>
      <Text testID="cart-title" style={styles.title}>{heading}</Text>
      {products.map((product) => (
        <View key={product.id} style={styles.row}>
          <View style={styles.product}>
            <Text style={styles.name}>{product.name}</Text>
            <Text>${(product.priceCents / 100).toFixed(2)} each</Text>
          </View>
          <Text testID={`quantity-${product.id}`}>{quantities[product.id]}</Text>
          <Pressable
            testID={`add-${product.id}`}
            accessibilityRole="button"
            accessibilityLabel={`Add ${product.name}`}
            style={styles.button}
            onPress={() => setQuantities((current) => ({
              ...current,
              [product.id]: current[product.id] + 1,
            }))}
          >
            <Text>Add</Text>
          </Pressable>
        </View>
      ))}
      <Text testID="cart-total" style={styles.total}>${(total / 100).toFixed(2)}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, padding: 24, paddingTop: 80, backgroundColor: '#fff', gap: 24 },
  title: { fontSize: 28, fontWeight: '600' },
  row: { flexDirection: 'row', alignItems: 'center', gap: 20 },
  product: { flex: 1, gap: 4 },
  name: { fontSize: 18 },
  button: { paddingHorizontal: 18, paddingVertical: 12, borderRadius: 8, backgroundColor: '#eee' },
  total: { fontSize: 24, fontWeight: '600' },
});
